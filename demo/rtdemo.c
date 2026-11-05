/*
 * rtdemo: a small real-time system with two hidden priority-inversion bugs.
 *
 *   control  prio 50  every 10 ms: gets a reading from sensor, then computes; must finish in 5 ms
 *   sensor   prio 10  produces readings, about 2 ms of CPU each
 *   logger   prio 30  background work: a 6 ms burst every 25 ms
 *
 * Message passing (good / bad): sensor is a server and control asks it with MsgSend. QNX normally
 * makes a server inherit the priority of the client it is serving, so sensor runs at 50 while it
 * handles control's request. "bad" creates the channel with _NTO_CHF_FIXED_PRIORITY, which turns
 * that off: sensor stays at 10, logger preempts it, and control sits SEND/REPLY-blocked behind a
 * lower-priority thread.
 *
 * Shared mutex (mutex-good / mutex-bad): sensor updates a shared reading with a mutex held for
 * about 2 ms, and control locks the same mutex to read it. QNX mutexes use priority inheritance
 * by default (PTHREAD_PRIO_INHERIT), so a priority 50 waiter lifts the owner to 50. "mutex-bad"
 * creates the mutex with PTHREAD_PRIO_NONE, which is the default on Linux: sensor keeps priority
 * 10 while it holds the lock, logger preempts it, and control waits MUTEX-blocked behind logger.
 * This is the bug that kept resetting Mars Pathfinder in 1997.
 *
 * Cross-process SMP (ipc-good / ipc-bad): control runs on CPU 1; a separate sensor process
 * and the logger run on CPU 0. Receiver rcvids and client tids are different namespaces.
 *
 * Usage: rtdemo good|bad|mutex-good|mutex-bad|ipc-good|ipc-bad [seconds]
 * The original four modes pin all threads to CPU 0; ipc modes require two CPUs.
 */
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/neutrino.h>
#include <sys/syspage.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CONTROL_PRIO 50
#define LOGGER_PRIO 30
#define SENSOR_PRIO 10
#define PERIOD_NS 10000000ULL
#define SENSOR_WORK_NS 2000000ULL
#define SENSOR_GAP_NS 2000000ULL
#define LOGGER_WORK_NS 6000000ULL
#define LOGGER_GAP_NS 25000000ULL
#define PULSE_TICK (_PULSE_CODE_MINAVAIL + 1)

static atomic_int stop;
static int sensor_chid;
static pid_t sensor_pid = -1;
static pthread_mutex_t state_lock;
static int shared_reading;

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void burn(uint64_t ns) {
    /* Busy work measured in thread CPU time, so being preempted doesn't shorten the work. */
    struct timespec ts;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    uint64_t end = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec + ns;
    volatile uint64_t x = 0;
    for (;;) {
        for (int i = 0; i < 200000; i++) x += (uint64_t)i * 31;
        clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
        if ((uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec >= end) break;
    }
}

static void check_errno(int result, const char *operation) {
    if (result == -1) { perror(operation); exit(1); }
}

static void check_pthread(int result, const char *operation) {
    if (result != 0) { fprintf(stderr, "%s: %s\n", operation, strerror(result)); exit(1); }
}

static void pin_to_cpu(unsigned cpu) {
    uintptr_t mask = (uintptr_t)1 << cpu;
    check_errno(ThreadCtl(_NTO_TCTL_RUNMASK, (void *)mask), "runmask");
}

static void set_prio(int prio) {
    struct sched_param param;
    memset(&param, 0, sizeof(param));
    param.sched_priority = prio;
    check_pthread(pthread_setschedparam(pthread_self(), SCHED_FIFO, &param), "setschedparam");
}

/* Message-passing modes: sensor is a server. */
static void *sensor_server(void *arg) {
    (void)arg;
    pthread_setname_np(pthread_self(), "sensor");
    pin_to_cpu(0);
    set_prio(SENSOR_PRIO);
    for (;;) {
        int request;
        rcvid_t rcvid = MsgReceive(sensor_chid, &request, sizeof(request), NULL);
        if (rcvid == -1) break;
        if (rcvid == 0) continue; /* pulse */
        burn(SENSOR_WORK_NS);
        int reading = request + 1;
        check_errno(MsgReply(rcvid, 0, &reading, sizeof(reading)), "MsgReply");
    }
    return NULL;
}

/* Mutex modes: sensor refreshes the shared reading with the lock held. */
static void *sensor_writer(void *arg) {
    (void)arg;
    pthread_setname_np(pthread_self(), "sensor");
    pin_to_cpu(0);
    set_prio(SENSOR_PRIO);
    while (!atomic_load_explicit(&stop, memory_order_relaxed)) {
        check_pthread(pthread_mutex_lock(&state_lock), "sensor lock");
        burn(SENSOR_WORK_NS);
        shared_reading++;
        check_pthread(pthread_mutex_unlock(&state_lock), "sensor unlock");
        struct timespec gap = {0, (long)SENSOR_GAP_NS};
        nanosleep(&gap, NULL);
    }
    return NULL;
}

static void *logger_thread(void *arg) {
    (void)arg;
    pthread_setname_np(pthread_self(), "logger");
    pin_to_cpu(0);
    set_prio(LOGGER_PRIO);
    while (!atomic_load_explicit(&stop, memory_order_relaxed)) {
        burn(LOGGER_WORK_NS);
        struct timespec gap = {0, (long)LOGGER_GAP_NS};
        nanosleep(&gap, NULL);
    }
    return NULL;
}

static void cleanup_sensor(void) {
    if (sensor_pid > 0) {
        kill(sensor_pid, SIGTERM);
        while (waitpid(sensor_pid, NULL, 0) == -1 && errno == EINTR) {}
        sensor_pid = -1;
    }
}

static int start_sensor_process(int bad) {
    int ready[2];
    check_errno(pipe(ready), "pipe");
    sensor_pid = fork();
    check_errno(sensor_pid, "fork");
    if (sensor_pid == 0) {
        close(ready[0]);
        sensor_chid = ChannelCreate(bad ? _NTO_CHF_FIXED_PRIORITY : 0);
        ssize_t count;
        do { count = write(ready[1], &sensor_chid, sizeof(sensor_chid)); } while (count == -1 && errno == EINTR);
        close(ready[1]);
        if (count != sizeof(sensor_chid) || sensor_chid == -1) _exit(1);
        sensor_server(NULL);
        _exit(0);
    }
    close(ready[1]);
    int chid;
    ssize_t count;
    do { count = read(ready[0], &chid, sizeof(chid)); } while (count == -1 && errno == EINTR);
    close(ready[0]);
    if (count != sizeof(chid) || chid < 0) {
        fprintf(stderr, "sensor process did not create its channel\n");
        exit(1);
    }
    return chid;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "";
    int bad = !strcmp(mode, "bad") || !strcmp(mode, "mutex-bad") || !strcmp(mode, "ipc-bad");
    int mutex_mode = !strcmp(mode, "mutex-good") || !strcmp(mode, "mutex-bad");
    int ipc_mode = !strcmp(mode, "ipc-good") || !strcmp(mode, "ipc-bad");
    if ((!bad && strcmp(mode, "good") && strcmp(mode, "mutex-good") && strcmp(mode, "ipc-good")) || argc > 3) {
        fprintf(stderr, "usage: %s good|bad|mutex-good|mutex-bad|ipc-good|ipc-bad [seconds]\n", argv[0]);
        return 2;
    }
    char *seconds_end;
    long seconds = argc > 2 ? strtol(argv[2], &seconds_end, 10) : 3;
    if (seconds < 1 || seconds > 60 || (argc > 2 && (*seconds_end || seconds_end == argv[2]))) {
        fprintf(stderr, "seconds must be a whole number from 1 to 60\n");
        return 2;
    }
    if (ipc_mode && _syspage_ptr->num_cpu < 2) {
        fprintf(stderr, "ipc modes require at least two CPUs\n");
        return 2;
    }
    atexit(cleanup_sensor);

    pthread_setname_np(pthread_self(), "control");
    pin_to_cpu(ipc_mode ? 1 : 0);
    set_prio(CONTROL_PRIO);

    int coid = -1;
    pthread_t sensor, logger;
    if (mutex_mode) {
        pthread_mutexattr_t attr;
        check_pthread(pthread_mutexattr_init(&attr), "mutexattr_init");
        if (bad) {
            /* Linux's default protocol. On QNX it switches off priority inheritance for this mutex. */
            check_pthread(pthread_mutexattr_setprotocol(&attr, PTHREAD_PRIO_NONE), "setprotocol");
        }
        check_pthread(pthread_mutex_init(&state_lock, &attr), "mutex_init");
        pthread_mutexattr_destroy(&attr);
        check_pthread(pthread_create(&sensor, NULL, sensor_writer, NULL), "create sensor");
    } else if (ipc_mode) {
        int chid = start_sensor_process(bad);
        coid = ConnectAttach(0, sensor_pid, chid, _NTO_SIDE_CHANNEL, 0);
        check_errno(coid, "ConnectAttach sensor process");
        printf("topology: control pid %ld CPU 1; sensor pid %ld CPU 0; logger CPU 0\n",
               (long)getpid(), (long)sensor_pid);
    } else {
        sensor_chid = ChannelCreate(bad ? _NTO_CHF_FIXED_PRIORITY : 0);
        if (sensor_chid == -1) {
            perror("ChannelCreate");
            return 1;
        }
        check_pthread(pthread_create(&sensor, NULL, sensor_server, NULL), "create sensor");
        coid = ConnectAttach(0, 0, sensor_chid, _NTO_SIDE_CHANNEL, 0);
        check_errno(coid, "ConnectAttach sensor");
    }
    check_pthread(pthread_create(&logger, NULL, logger_thread, NULL), "create logger");

    int tick_chid = ChannelCreate(0);
    check_errno(tick_chid, "ChannelCreate timer");
    int tick_coid = ConnectAttach(0, 0, tick_chid, _NTO_SIDE_CHANNEL, 0);
    check_errno(tick_coid, "ConnectAttach timer");
    struct sigevent event;
    SIGEV_PULSE_INIT(&event, tick_coid, CONTROL_PRIO, PULSE_TICK, 0);
    timer_t timer;
    check_errno(timer_create(CLOCK_MONOTONIC, &event, &timer), "timer_create");
    struct itimerspec spec = {{0, (long)PERIOD_NS}, {0, (long)PERIOD_NS}};
    check_errno(timer_settime(timer, 0, &spec, NULL), "timer_settime");

    unsigned long cycles = 0, misses = 0;
    uint64_t worst = 0, total = 0;
    uint64_t end = now_ns() + (uint64_t)seconds * 1000000000ULL;
    while (now_ns() < end) {
        struct _pulse pulse;
        check_errno(MsgReceivePulse(tick_chid, &pulse, sizeof(pulse), NULL), "MsgReceivePulse");
        uint64_t t0 = now_ns();
        int request = (int)cycles, reading = 0;
        if (mutex_mode) {
            check_pthread(pthread_mutex_lock(&state_lock), "control lock");
            reading = shared_reading;
            burn(100000ULL);
            check_pthread(pthread_mutex_unlock(&state_lock), "control unlock");
        } else {
            check_errno(MsgSend(coid, &request, sizeof(request), &reading, sizeof(reading)), "MsgSend");
        }
        burn(500000ULL);
        uint64_t took = now_ns() - t0;
        cycles++;
        total += took;
        if (took > worst) worst = took;
        if (took > PERIOD_NS / 2) misses++; /* the cycle must finish within 5 ms */
    }
    atomic_store_explicit(&stop, 1, memory_order_relaxed);
    timer_delete(timer);
    check_pthread(pthread_join(logger, NULL), "join logger");
    if (!ipc_mode) {
        if (!mutex_mode) ChannelDestroy(sensor_chid);
        check_pthread(pthread_join(sensor, NULL), "join sensor");
    }
    printf("rtdemo %s: %lu cycles, %lu over the 5 ms budget, mean %.2f ms, worst %.2f ms\n", mode, cycles, misses,
           total / 1e6 / (cycles ? cycles : 1), worst / 1e6);
    return 0;
}
