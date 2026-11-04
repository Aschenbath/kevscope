/*
 * rtdemo: a small real-time system with a hidden message-passing bug.
 *
 *   control  prio 50  every 10 ms: asks sensor for a reading (MsgSend), then computes
 *   sensor   prio 10  server: each request takes about 2 ms of CPU
 *   logger   prio 30  background work: a 6 ms burst every 25 ms
 *
 * QNX normally makes a server inherit the priority of the client it is serving, so sensor runs at
 * 50 while it handles control's request. "bad" mode creates the channel with
 * _NTO_CHF_FIXED_PRIORITY, which turns that off: sensor stays at 10, logger preempts it, and
 * control sits SEND/REPLY-blocked behind a lower-priority thread. That is a priority inversion.
 *
 * Usage: rtdemo good|bad [seconds]
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
#define LOGGER_WORK_NS 6000000ULL
#define LOGGER_GAP_NS 25000000ULL
#define PULSE_TICK (_PULSE_CODE_MINAVAIL + 1)

static volatile int stop;
static int sensor_chid;
static int bad_mode;

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

static void *sensor_thread(void *arg) {
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
    if (argc < 2 || (strcmp(argv[1], "good") && strcmp(argv[1], "bad"))) {
        fprintf(stderr, "usage: %s good|bad [seconds]\n", argv[0]);
        return 2;
    }
    bad_mode = !strcmp(argv[1], "bad");
    int seconds = argc > 2 ? atoi(argv[2]) : 3;

    pthread_setname_np(pthread_self(), "control");
    pin_to_cpu0();
    set_prio(CONTROL_PRIO);

    sensor_chid = ChannelCreate(bad_mode ? _NTO_CHF_FIXED_PRIORITY : 0);
    if (sensor_chid == -1) {
        perror("ChannelCreate");
        return 1;
    }
    pthread_t sensor, logger;
    pthread_create(&sensor, NULL, sensor_thread, NULL);
    pthread_create(&logger, NULL, logger_thread, NULL);

    int coid = ConnectAttach(0, 0, sensor_chid, _NTO_SIDE_CHANNEL, 0);
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
        MsgSend(coid, &request, sizeof(request), &reading, sizeof(reading));
        burn(500000ULL);
        uint64_t took = now_ns() - t0;
        cycles++;
        total += took;
        if (took > worst) worst = took;
        if (took > PERIOD_NS / 2) misses++; /* the cycle must finish within 5 ms */
    }
    stop = 1;
    printf("rtdemo %s: %lu cycles, %lu over the 5 ms budget, mean %.2f ms, worst %.2f ms\n", argv[1], cycles, misses,
           total / 1e6 / (cycles ? cycles : 1), worst / 1e6);
    return 0;
}
