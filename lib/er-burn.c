// er-burn: self-checking CPU load for the overclock safety test (elden-research).
//
//   er-burn CPUS SECONDS [LOAD]     e.g.  er-burn 2 30     er-burn 8-15 120 20
//
// Runs one thread per CPU in CPUS, each pinned to its CPU. Each thread repeats a fixed
// block of work: integer hashing over a 192 KB buffer (it stays in the L2 cache and
// does data-dependent loads), then AVX2 FMA chains interleaved with integer ALU chains
// (the high-current part). Every repetition must give the same 64-bit checksum as the
// thread's first one. The FP part is chaotic, so one wrong bit anywhere in the block
// changes the result. A mismatch means the core computed something wrong: the clock
// and voltage aren't stable.
//
// Calibration on a BC-250 at stock clocks: 4 CPUs at 100% reach 76 C (stress --cpu:
// 83 C); the OC test's game-like mix reaches 65 C (Elden Ring itself: 68-72 C, since
// its GPU load heats the same die).
//
// LOAD (1-100, default 100) is the share of time each thread works. It sleeps the
// rest of the time, which makes a game-like load.
// Prints "er-burn: B blocks, E errors" at the end. Exit status: 0 = no errors,
// 3 = errors, 1 = bad arguments or a CPU that can't be used.
// Build: gcc -O3 -march=native -pthread -o er-burn er-burn.c
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define WORDS (192 * 1024 / 8)
#define STEPS 40000

typedef double v4 __attribute__((vector_size(32)));

struct job {
  int cpu, failed;
  long blocks, errors;
};

static double duration, duty = 1;

static double now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec * 1e-9;
}

static uint64_t block(uint64_t *buf) {
  uint64_t s = 0x9E3779B97F4A7C15ull, h = 0;
  for (int i = 0; i < WORDS; i++) {
    s ^= s << 13;
    s ^= s >> 7;
    s ^= s << 17;
    buf[i] = s;
  }
  for (int i = 0; i < WORDS; i++) {
    uint64_t v = buf[buf[i] % WORDS];
    buf[i] = (buf[i] ^ (v >> 29)) * 0xD6E8FEB86659FD93ull + (v << 7);
    h = (h ^ buf[i]) * 0x100000001B3ull;
  }
  // The high-current part: a logistic map at r = 3.99 in 32 lanes held in registers
  // (8 independent chains keep both FMA pipes busy), interleaved with 8 integer
  // chains for the ALUs. The map is chaotic, so an error grows instead of fading out.
  v4 x[8];
  uint64_t a[8];
  for (int c = 0; c < 8; c++) {
    a[c] = s + c;
    for (int l = 0; l < 4; l++)
      x[c][l] = 0.1 + 0.8 * (c * 4 + l) / 32;
  }
  for (int k = 0; k < STEPS; k++)
    for (int c = 0; c < 8; c++) {
      v4 t = 3.99 * x[c];
      x[c] = t - t * x[c];
      a[c] = ((a[c] << 7 | a[c] >> 57) ^ a[(c + 1) & 7]) + (a[c] >> 3) + k;
    }
  for (int c = 0; c < 8; c++)
    h = (h ^ a[c]) * 0x100000001B3ull;
  for (int c = 0; c < 8; c++)
    for (int l = 0; l < 4; l++) {
      uint64_t b;
      memcpy(&b, &x[c][l], sizeof b);
      h = (h ^ b) * 0x100000001B3ull;
    }
  return h;
}

static void *worker(void *arg) {
  struct job *j = arg;
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(j->cpu, &set);
  uint64_t *buf = malloc(WORDS * sizeof *buf);
  if (!buf || pthread_setaffinity_np(pthread_self(), sizeof set, &set)) {
    fprintf(stderr, "er-burn: can't run on CPU %d\n", j->cpu);
    j->failed = 1;
    return NULL;
  }
  uint64_t ref = block(buf);
  double end = now() + duration;
  while (now() < end) {
    double t0 = now();
    if (block(buf) != ref && ++j->errors <= 5)
      fprintf(stderr, "er-burn: CPU %d: wrong result in block %ld\n", j->cpu, j->blocks + 1);
    j->blocks++;
    if (duty < 1) {
      double rest = (now() - t0) * (1 - duty) / duty;
      struct timespec ts = {(time_t)rest, (long)((rest - (time_t)rest) * 1e9)};
      nanosleep(&ts, NULL);
    }
  }
  free(buf);
  return NULL;
}

// "0-1,4-7" -> {0, 1, 4, 5, 6, 7}; returns the count, or -1 if malformed.
static int parse_cpus(const char *s, int *out, int max) {
  int n = 0;
  while (*s) {
    char *e;
    long a = strtol(s, &e, 10), b = a;
    if (e == s || a < 0)
      return -1;
    if (*e == '-') {
      s = e + 1;
      b = strtol(s, &e, 10);
      if (e == s || b < a)
        return -1;
    }
    for (long c = a; c <= b; c++) {
      if (n == max)
        return -1;
      out[n++] = (int)c;
    }
    if (*e == ',')
      e++;
    else if (*e)
      return -1;
    s = e;
  }
  return n;
}

int main(int argc, char **argv) {
  int cpus[1024], n;
  if (argc < 3 || (n = parse_cpus(argv[1], cpus, 1024)) <= 0 || (duration = atof(argv[2])) <= 0) {
    fprintf(stderr, "usage: er-burn CPUS SECONDS [LOAD 1-100]\n");
    return 1;
  }
  if (argc > 3) {
    duty = atof(argv[3]) / 100;
    if (duty <= 0 || duty > 1) {
      fprintf(stderr, "er-burn: LOAD must be 1-100\n");
      return 1;
    }
  }
  struct job *jobs = calloc(n, sizeof *jobs);
  pthread_t *threads = calloc(n, sizeof *threads);
  for (int i = 0; i < n; i++) {
    jobs[i].cpu = cpus[i];
    pthread_create(&threads[i], NULL, worker, &jobs[i]);
  }
  long blocks = 0, errors = 0;
  int failed = 0;
  for (int i = 0; i < n; i++) {
    pthread_join(threads[i], NULL);
    blocks += jobs[i].blocks;
    errors += jobs[i].errors;
    failed |= jobs[i].failed;
    if (jobs[i].errors)
      printf("er-burn: CPU %d: %ld errors in %ld blocks\n", jobs[i].cpu, jobs[i].errors, jobs[i].blocks);
  }
  printf("er-burn: %ld blocks, %ld errors\n", blocks, errors);
  return failed ? 1 : errors ? 3 : 0;
}
