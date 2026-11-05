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
 * Usage: rtdemo good|bad|mutex-good|mutex-bad [seconds]
 * All threads are pinned to CPU 0 so the contention doesn't depend on the number of CPUs.
 */
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/neutrino.h>
#include <sys/syspage.h>
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

static volatile int stop;
static int sensor_chid;
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

static void pin_to_cpu0(void) {
    uintptr_t mask = 1;
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)mask) == -1) perror("runmask");
}

static void set_prio(int prio) {
    struct sched_param param;
    memset(&param, 0, sizeof(param));
    param.sched_priority = prio;
    int rc = pthread_setschedparam(pthread_self(), SCHED_FIFO, &param);
    if (rc != 0) fprintf(stderr, "setschedparam: %s\n", strerror(rc));
}

/* Message-passing modes: sensor is a server. */
static void *sensor_server(void *arg) {
    (void)arg;
    pthread_setname_np(pthread_self(), "sensor");
    pin_to_cpu0();
    set_prio(SENSOR_PRIO);
    for (;;) {
        int request;
        rcvid_t rcvid = MsgReceive(sensor_chid, &request, sizeof(request), NULL);
        if (rcvid == -1) break;
        if (rcvid == 0) continue; /* pulse */
        burn(SENSOR_WORK_NS);
        int reading = request + 1;
        MsgReply(rcvid, 0, &reading, sizeof(reading));
    }
    return NULL;
}

/* Mutex modes: sensor refreshes the shared reading with the lock held. */
static void *sensor_writer(void *arg) {
    (void)arg;
    pthread_setname_np(pthread_self(), "sensor");
    pin_to_cpu0();
    set_prio(SENSOR_PRIO);
    while (!stop) {
        pthread_mutex_lock(&state_lock);
        burn(SENSOR_WORK_NS);
        shared_reading++;
        pthread_mutex_unlock(&state_lock);
        struct timespec gap = {0, (long)SENSOR_GAP_NS};
        nanosleep(&gap, NULL);
    }
    return NULL;
}

static void *logger_thread(void *arg) {
    (void)arg;
    pthread_setname_np(pthread_self(), "logger");
    pin_to_cpu0();
    set_prio(LOGGER_PRIO);
    while (!stop) {
        burn(LOGGER_WORK_NS);
        struct timespec gap = {0, (long)LOGGER_GAP_NS};
        nanosleep(&gap, NULL);
    }
    return NULL;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "";
    int bad = !strcmp(mode, "bad") || !strcmp(mode, "mutex-bad");
    int mutex_mode = !strncmp(mode, "mutex-", 6);
    if (!bad && strcmp(mode, "good") && strcmp(mode, "mutex-good")) {
        fprintf(stderr, "usage: %s good|bad|mutex-good|mutex-bad [seconds]\n", argv[0]);
        return 2;
    }
    int seconds = argc > 2 ? atoi(argv[2]) : 3;

    pthread_setname_np(pthread_self(), "control");
    pin_to_cpu0();
    set_prio(CONTROL_PRIO);

    int coid = -1;
    pthread_t sensor, logger;
    if (mutex_mode) {
        pthread_mutexattr_t attr;
        pthread_mutexattr_init(&attr);
        if (bad) {
            /* Linux's default protocol. On QNX it switches off priority inheritance for this mutex. */
            int rc = pthread_mutexattr_setprotocol(&attr, PTHREAD_PRIO_NONE);
            if (rc != 0) fprintf(stderr, "setprotocol: %s\n", strerror(rc));
        }
        pthread_mutex_init(&state_lock, &attr);
        pthread_mutexattr_destroy(&attr);
        pthread_create(&sensor, NULL, sensor_writer, NULL);
    } else {
        sensor_chid = ChannelCreate(bad ? _NTO_CHF_FIXED_PRIORITY : 0);
        if (sensor_chid == -1) {
            perror("ChannelCreate");
            return 1;
        }
        pthread_create(&sensor, NULL, sensor_server, NULL);
        coid = ConnectAttach(0, 0, sensor_chid, _NTO_SIDE_CHANNEL, 0);
    }
    pthread_create(&logger, NULL, logger_thread, NULL);

    int tick_chid = ChannelCreate(0);
    int tick_coid = ConnectAttach(0, 0, tick_chid, _NTO_SIDE_CHANNEL, 0);
    struct sigevent event;
    SIGEV_PULSE_INIT(&event, tick_coid, CONTROL_PRIO, PULSE_TICK, 0);
    timer_t timer;
    timer_create(CLOCK_MONOTONIC, &event, &timer);
    struct itimerspec spec = {{0, (long)PERIOD_NS}, {0, (long)PERIOD_NS}};
    timer_settime(timer, 0, &spec, NULL);

    unsigned long cycles = 0, misses = 0;
    uint64_t worst = 0, total = 0;
    uint64_t end = now_ns() + (uint64_t)seconds * 1000000000ULL;
    while (now_ns() < end) {
        struct _pulse pulse;
        MsgReceivePulse(tick_chid, &pulse, sizeof(pulse), NULL);
        uint64_t t0 = now_ns();
        int request = (int)cycles, reading = 0;
        if (mutex_mode) {
            pthread_mutex_lock(&state_lock);
            reading = shared_reading;
            burn(100000ULL);
            pthread_mutex_unlock(&state_lock);
        } else {
            MsgSend(coid, &request, sizeof(request), &reading, sizeof(reading));
        }
        burn(500000ULL);
        uint64_t took = now_ns() - t0;
        cycles++;
        total += took;
        if (took > worst) worst = took;
        if (took > PERIOD_NS / 2) misses++; /* the cycle must finish within 5 ms */
    }
    stop = 1;
    printf("rtdemo %s: %lu cycles, %lu over the 5 ms budget, mean %.2f ms, worst %.2f ms\n", mode, cycles, misses,
           total / 1e6 / (cycles ? cycles : 1), worst / 1e6);
    return 0;
}
