// Memory latency (4K vs 2M pages), per-CCX read bandwidth, core-to-core latency.
// Build: gcc -O2 -march=native -pthread -o membench membench.c
// "./membench quick" prints one key=value line (used by er-smu.py profile-test).
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>

static double now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec * 1e-9;
}

static void pin(int cpu) {
  cpu_set_t s;
  CPU_ZERO(&s);
  CPU_SET(cpu, &s);
  pthread_setaffinity_np(pthread_self(), sizeof s, &s);
}

static char *alloc(size_t sz, int huge) {
  size_t al = 2UL << 20;
  char *p = mmap(NULL, sz + al, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  char *a = (char *)(((uintptr_t)p + al - 1) & ~(al - 1));
  madvise(a, sz, huge ? MADV_HUGEPAGE : MADV_NOHUGEPAGE);
  memset(a, 1, sz);
  return a;
}

static long anon_huge_kb(void) {
  FILE *f = fopen("/proc/self/smaps_rollup", "r");
  char line[256];
  long kb = -1;
  while (f && fgets(line, sizeof line, f))
    if (sscanf(line, "AnonHugePages: %ld", &kb) == 1) break;
  if (f) fclose(f);
  return kb;
}

// Random pointer chase over cache lines: ns per dependent load.
static double latency(size_t sz, int huge, long *thp_kb) {
  size_t n = sz / 64;
  char *buf = alloc(sz, huge);
  *thp_kb = anon_huge_kb();
  uint32_t *perm = malloc(n * sizeof *perm);
  for (size_t i = 0; i < n; i++) perm[i] = i;
  uint64_t x = 88172645463325252ULL;
  for (size_t i = n - 1; i > 0; i--) {
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    size_t j = x % (i + 1);
    uint32_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
  }
  for (size_t i = 0; i < n; i++)
    *(void **)(buf + (size_t)perm[i] * 64) = buf + (size_t)perm[(i + 1) % n] * 64;
  free(perm);
  void **p = (void **)buf;
  size_t iters = 10000000;
  for (size_t i = 0; i < (n < iters ? n : iters); i++) p = *p;
  double t0 = now();
  for (size_t i = 0; i < iters; i++) p = *p;
  double t1 = now();
  if (!p) puts("");
  munmap(buf, sz);
  return (t1 - t0) / iters * 1e9;
}

// Memory-level parallelism: `chains` independent random chases over 1 GB (2M pages),
// each step padded with `filler` independent adds. With filler, the number of loads
// in flight is bounded by reorder-buffer capacity (halved if SMT partitions it).
static double mlp(int chains, int filler) {
  size_t sz = 1UL << 30, n = sz / 64;
  char *buf = alloc(sz, 1);
  uint32_t *perm = malloc(n * sizeof *perm);
  for (size_t i = 0; i < n; i++) perm[i] = i;
  uint64_t x = 88172645463325252ULL;
  for (size_t i = n - 1; i > 0; i--) {
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    size_t j = x % (i + 1);
    uint32_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
  }
  for (size_t i = 0; i < n; i++)
    *(void **)(buf + (size_t)perm[i] * 64) = buf + (size_t)perm[(i + 1) % n] * 64;
  void *p[16];
  for (int j = 0; j < chains; j++) p[j] = buf + (size_t)perm[n / chains * j] * 64;
  free(perm);
  uint64_t r0 = 0, r1 = 0, r2 = 0, r3 = 0, r4 = 0, r5 = 0, r6 = 0, r7 = 0;
  size_t steps = 4000000;
  double t0 = now();
  for (size_t s = 0; s < steps; s++)
    for (int j = 0; j < chains; j++) {
      p[j] = *(void **)p[j];
      if (filler)
        __asm__ volatile("add $1,%0\n\tadd $1,%1\n\tadd $1,%2\n\tadd $1,%3\n\t"
                         "add $1,%4\n\tadd $1,%5\n\tadd $1,%6\n\tadd $1,%7\n\t"
                         "add $1,%0\n\tadd $1,%1\n\tadd $1,%2\n\tadd $1,%3\n\t"
                         "add $1,%4\n\tadd $1,%5\n\tadd $1,%6\n\tadd $1,%7"
                         : "+r"(r0), "+r"(r1), "+r"(r2), "+r"(r3), "+r"(r4), "+r"(r5), "+r"(r6), "+r"(r7));
    }
  double t1 = now();
  if (!p[0] || r0 + r1 + r2 + r3 + r4 + r5 + r6 + r7 == 1) puts("");
  munmap(buf, sz);
  return (t1 - t0) / ((double)steps * chains) * 1e9;
}

struct bw { int cpu; size_t sz; double gbs; pthread_barrier_t *bar; };

static void *bw_thread(void *arg) {
  struct bw *b = arg;
  pin(b->cpu);
  uint64_t *buf = (uint64_t *)alloc(b->sz, 1), sum = 0;
  size_t n = b->sz / 8;
  pthread_barrier_wait(b->bar);
  double t0 = now();
  for (int r = 0; r < 8; r++)
    for (size_t i = 0; i < n; i += 4) sum += buf[i] ^ buf[i + 1] ^ buf[i + 2] ^ buf[i + 3];
  double t1 = now();
  b->gbs = 8.0 * b->sz / (t1 - t0) / 1e9;
  if (sum == 42) puts("");
  return NULL;
}

static double bandwidth(const char *label, const int *cpus, int k) {
  pthread_t th[16];
  struct bw b[16];
  pthread_barrier_t bar;
  pthread_barrier_init(&bar, NULL, k);
  for (int i = 0; i < k; i++) {
    b[i] = (struct bw){cpus[i], 64UL << 20, 0, &bar};  // 16x the L3; 1 GB total with 16 threads
    pthread_create(&th[i], NULL, bw_thread, &b[i]);
  }
  double tot = 0;
  for (int i = 0; i < k; i++) { pthread_join(th[i], NULL); tot += b[i].gbs; }
  if (label) printf("  %-34s %6.1f GB/s\n", label, tot);
  return tot;
}

static _Atomic int flag;
struct pp { int cpu; int iters; };

static void *pong(void *arg) {
  struct pp *p = arg;
  pin(p->cpu);
  for (int i = 0; i < p->iters; i++) {
    while (atomic_load_explicit(&flag, memory_order_acquire) != 1) ;
    atomic_store_explicit(&flag, 0, memory_order_release);
  }
  return NULL;
}

static double pingpong(int a, int b) {
  struct pp p = {b, 200000};
  pthread_t t;
  atomic_store(&flag, 0);
  pthread_create(&t, NULL, pong, &p);
  pin(a);
  double t0 = now();
  for (int i = 0; i < p.iters; i++) {
    atomic_store_explicit(&flag, 1, memory_order_release);
    while (atomic_load_explicit(&flag, memory_order_acquire) != 0) ;
  }
  double t1 = now();
  pthread_join(t, NULL);
  return (t1 - t0) / p.iters / 2 * 1e9;  // one-way
}

int main(int argc, char **argv) {
  pin(2);
  if (argc > 1 && !strcmp(argv[1], "mlp")) {
    printf("1 chain %.1f ns, 16 chains %.1f ns, 16 chains + filler %.1f ns per load\n",
           mlp(1, 0), mlp(16, 0), mlp(16, 16));
    return 0;
  }
  if (argc > 1 && !strcmp(argv[1], "quick")) {
    int all[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
    long kb;
    double l2 = latency(1UL << 30, 1, &kb), l4 = latency(1UL << 30, 0, &kb);
    double l256 = latency(256UL << 20, 1, &kb), bw = bandwidth(NULL, all, 16);
    pin(2);
    printf("lat_2m_1g=%.1f lat_4k_1g=%.1f lat_2m_256m=%.1f bw_all=%.1f xccx=%.1f\n",
           l2, l4, l256, bw, pingpong(2, 8));
    return 0;
  }
  size_t sizes[] = {16 << 10, 256 << 10, 2 << 20, 8 << 20, 64 << 20, 256 << 20, 1UL << 30};
  puts("Latency, pinned to CPU 2 (ns per dependent load):");
  puts("  size        4K pages   2M pages   (THP backing of the 2M run)");
  for (int i = 0; i < 7; i++) {
    long k4, k2;
    double l4 = latency(sizes[i], 0, &k4), l2 = latency(sizes[i], 1, &k2);
    printf("  %7zu KB  %8.1f   %8.1f   (%ld MB huge)\n", sizes[i] >> 10, l4, l2, k2 >> 10);
  }
  puts("Read bandwidth:");
  int one[] = {2}, ccx0[] = {0, 2, 4, 6}, ccx0s[] = {0, 1, 2, 3, 4, 5, 6, 7};
  int ccx1[] = {8, 10, 12, 14}, both[] = {0, 2, 4, 6, 8, 10, 12, 14};
  int all[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
  bandwidth("1 thread (CPU 2)", one, 1);
  bandwidth("CCX0, 4 cores (0,2,4,6)", ccx0, 4);
  bandwidth("CCX0, 8 threads (0-7)", ccx0s, 8);
  bandwidth("CCX1, 4 cores (8,10,12,14)", ccx1, 4);
  bandwidth("both CCXs, 8 cores", both, 8);
  bandwidth("all 16 threads", all, 16);
  puts("Core-to-core one-way latency (ns):");
  printf("  CPU 2 <-> 3  (SMT sibling)   %6.1f\n", pingpong(2, 3));
  printf("  CPU 2 <-> 4  (same CCX)      %6.1f\n", pingpong(2, 4));
  printf("  CPU 2 <-> 8  (other CCX)     %6.1f\n", pingpong(2, 8));
  return 0;
}
