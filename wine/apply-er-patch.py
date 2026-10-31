#!/usr/bin/env python3
"""er-wine patch 1: main-thread spin-before-block + NtCreateEvent caller log.

Edits a wine-cachyos source tree (commit cd14106e) in place by exact text
replacement; every anchor must match exactly once, or nothing is written.
The resulting `git diff` is saved as 0001-er-main-thread-spin.patch, which is
what the build script applies.

  python3 -I apply-er-patch.py ~/.cache/elden-research/wine/src

Environment variables read by the patched ntdll.so (all off when unset/0):
  WINE_SERVER_SPIN_US=N  main thread: poll the wineserver reply pipe up to N us before blocking
  WINE_SYNC_SPIN_US=N    main thread: re-check futex alerts / ntsync objects up to N us before blocking
  WINE_ER_LOG=<file>     every 10 s: spin hit rates, NtCreateEvent counts, callers, lifetimes
  WINE_ER_EXE=<name>     only in the process whose exe path ends with <name> (e.g. eldenring.exe)
"""
import sys

SRC = sys.argv[1]
U = SRC + "/dlls/ntdll/unix/"

ER_BLOCK = r'''

/***********************************************************************
 * er-wine: Elden Ring tuning (see RESEARCH.md in elden-research).
 *
 * The game's main thread loses ~13% of its time asleep or waiting to be
 * scheduled after short waits: ~7.6K wineserver round trips/s, plus futex
 * (critical section, SRW lock) and ntsync waits that end within microseconds.
 * For the process's main thread only, spin briefly before blocking:
 *   WINE_SERVER_SPIN_US=N  poll the server reply pipe for up to N us
 *   WINE_SYNC_SPIN_US=N    re-check a futex alert or ntsync objects for up to N us
 *   WINE_ER_LOG=<file>     every 10 s, write spin hit rates and who creates events
 *   WINE_ER_EXE=<name>     only in the process whose exe path ends with <name>
 * Unset or 0 means stock behavior.
 */

TEB *er_main_teb;
ULONGLONG er_server_spin_ns;
ULONGLONG er_sync_spin_ns;
BOOL er_log_on;

#define ER_DEPTH   4     /* code addresses kept per NtCreateEvent caller */
#define ER_NSIG    256   /* distinct callers */
#define ER_NHANDLE 4096  /* recently created events, for lifetimes (direct-mapped) */

static char *er_log_path;
static struct
{
    ULONGLONG start, last_dump;
    struct { ULONG hit, miss; ULONGLONG hit_ns, miss_ns; } spin[3];  /* server reply, futex, ntsync */
    ULONG creates, kind[2][2], named, attrs, sig_lost;
    LONG creates_other;
    ULONG closes_own, closes_other, life[7];
} er;
static struct { ULONG_PTR addr[ER_DEPTH]; ULONG count; } er_sig[ER_NSIG];
static struct { HANDLE handle; ULONGLONG time; } er_live[ER_NHANDLE];

static ULONGLONG er_now_ns(void)
{
    struct timespec ts;

    clock_gettime( CLOCK_MONOTONIC, &ts );
    return ts.tv_sec * (ULONGLONG)1000000000 + ts.tv_nsec;
}

static ULONGLONG er_env_us( const char *name )
{
    const char *s = getenv( name );
    ULONGLONG us = s ? strtoull( s, NULL, 10 ) : 0;

    return (us > 1000 ? 1000 : us) * 1000;
}

static BOOL er_ends_with( const char *str, const char *end )
{
    size_t len = strlen( str ), n = strlen( end ), i;

    if (len < n) return FALSE;
    for (i = 0; i < n; i++)
    {
        char a = str[len - n + i], b = end[i];
        if (a >= 'A' && a <= 'Z') a += 'a' - 'A';
        if (b >= 'A' && b <= 'Z') b += 'a' - 'A';
        if (a != b) return FALSE;
    }
    return TRUE;
}

/* called by hacks_init() on the process's first thread */
void er_init( const char *exe )
{
    const char *want = getenv( "WINE_ER_EXE" ), *log = getenv( "WINE_ER_LOG" );

    if (want && *want && (!exe || !er_ends_with( exe, want ))) return;
    er_server_spin_ns = er_env_us( "WINE_SERVER_SPIN_US" );
    er_sync_spin_ns = er_env_us( "WINE_SYNC_SPIN_US" );
    if (log && *log) er_log_path = strdup( log );
    if (!er_server_spin_ns && !er_sync_spin_ns && !er_log_path) return;
    er.start = er.last_dump = er_now_ns();
    er_main_teb = NtCurrentTeb();
    er_log_on = er_log_path != NULL;
    ERR( "HACK: er-wine: main thread spins: server reply %u us, sync %u us; log %s\n",
         (unsigned int)(er_server_spin_ns / 1000), (unsigned int)(er_sync_spin_ns / 1000),
         er_log_path ? er_log_path : "off" );
}

static void er_note_spin( int kind, BOOL hit, ULONGLONG ns )
{
    if (!er_log_on) return;
    if (hit)
    {
        er.spin[kind].hit++;
        er.spin[kind].hit_ns += ns;
    }
    else
    {
        er.spin[kind].miss++;
        er.spin[kind].miss_ns += ns;
    }
}

static void er_append( char *buf, size_t size, size_t *len, const char *format, ... ) __attribute__((format(printf, 4, 5)));
static void er_append( char *buf, size_t size, size_t *len, const char *format, ... )
{
    va_list args;
    int n;

    if (*len >= size - 1) return;
    va_start( args, format );
    n = vsnprintf( buf + *len, size - *len, format, args );
    va_end( args );
    if (n > 0) *len += n;
    if (*len > size - 1) *len = size - 1;
}

static void er_dump(void)
{
    static const char * const spins[3] = { "server reply", "futex alert", "ntsync wait" };
    static const char * const lives[7] = { "<10us", "<100us", "<1ms", "<16.7ms", "<100ms", "<1s", ">=1s" };
    static char buf[32768], path[4200];
    unsigned long long secs = (er_now_ns() - er.start) / 1000000000;
    BOOL used[ER_NSIG] = { 0 };
    size_t len = 0;
    ULONG i, j, best;
    ssize_t written;
    int fd;

    er_append( buf, sizeof(buf), &len, "er-wine log: pid %d, %llu s; spin limits: server reply %u us, sync %u us\n",
               getpid(), secs, (unsigned int)(er_server_spin_ns / 1000), (unsigned int)(er_sync_spin_ns / 1000) );
    for (i = 0; i < 3; i++)
        er_append( buf, sizeof(buf), &len, "%s spin: hit %u (avg %llu ns), miss %u (avg %llu ns)\n", spins[i],
                   (unsigned int)er.spin[i].hit, (unsigned long long)(er.spin[i].hit ? er.spin[i].hit_ns / er.spin[i].hit : 0),
                   (unsigned int)er.spin[i].miss, (unsigned long long)(er.spin[i].miss ? er.spin[i].miss_ns / er.spin[i].miss : 0) );
    er_append( buf, sizeof(buf), &len, "NtCreateEvent: main thread %u (%llu/s), other threads %d\n",
               (unsigned int)er.creates, secs ? er.creates / secs : 0ULL, (int)er.creates_other );
    er_append( buf, sizeof(buf), &len, "  manual-reset: %u unsignaled, %u signaled; auto-reset: %u unsignaled, %u signaled; "
               "named %u; root dir / security descriptor / inherit %u\n",
               (unsigned int)er.kind[1][0], (unsigned int)er.kind[1][1], (unsigned int)er.kind[0][0],
               (unsigned int)er.kind[0][1], (unsigned int)er.named, (unsigned int)er.attrs );
    er_append( buf, sizeof(buf), &len, "NtClose on main thread: %u of events it created, %u other handles; lifetimes:",
               (unsigned int)er.closes_own, (unsigned int)er.closes_other );
    for (i = 0; i < 7; i++) er_append( buf, sizeof(buf), &len, " %s %u", lives[i], (unsigned int)er.life[i] );
    er_append( buf, sizeof(buf), &len, "\nNtCreateEvent callers (count, then code addresses on the PE stack, innermost first; "
               "%u not recorded):\n", (unsigned int)er.sig_lost );
    for (j = 0; j < 40; j++)
    {
        for (best = ER_NSIG, i = 0; i < ER_NSIG; i++)
            if (er_sig[i].count && !used[i] && (best == ER_NSIG || er_sig[i].count > er_sig[best].count)) best = i;
        if (best == ER_NSIG) break;
        used[best] = TRUE;
        er_append( buf, sizeof(buf), &len, "%10u", (unsigned int)er_sig[best].count );
        for (i = 0; i < ER_DEPTH && er_sig[best].addr[i]; i++)
            er_append( buf, sizeof(buf), &len, " %p", (void *)er_sig[best].addr[i] );
        er_append( buf, sizeof(buf), &len, "\n" );
    }

    if (snprintf( path, sizeof(path), "%s.tmp", er_log_path ) >= (int)sizeof(path)) return;
    if ((fd = open( path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644 )) == -1) return;
    written = write( fd, buf, len );
    close( fd );
    if (written == (ssize_t)len) rename( path, er_log_path );
}

static void er_note_event_create( const OBJECT_ATTRIBUTES *attr, EVENT_TYPE type, BOOLEAN state, HANDLE handle )
{
    ULONG_PTR addr[ER_DEPTH] = { 0 };
    ULONG i, n, hash = 0;
    ULONGLONG now;

    if (NtCurrentTeb() != er_main_teb)
    {
        InterlockedIncrement( &er.creates_other );
        return;
    }
    now = er_now_ns();
    er.creates++;
    er.kind[type == NotificationEvent][state ? 1 : 0]++;
    if (attr && attr->ObjectName && attr->ObjectName->Length) er.named++;
    if (attr && (attr->RootDirectory || attr->SecurityDescriptor || (attr->Attributes & OBJ_INHERIT))) er.attrs++;
    i = ((ULONG_PTR)handle >> 2) & (ER_NHANDLE - 1);
    er_live[i].handle = handle;
    er_live[i].time = now;

    n = er_get_pe_callers( addr, ER_DEPTH );
    for (i = 0; i < n; i++) hash = hash * 31 + (ULONG)(addr[i] ^ (addr[i] >> 32));
    for (i = 0; i < ER_NSIG; i++)
    {
        ULONG slot = (hash + i) % ER_NSIG;

        if (!er_sig[slot].count) memcpy( er_sig[slot].addr, addr, sizeof(addr) );
        else if (memcmp( er_sig[slot].addr, addr, sizeof(addr) )) continue;
        er_sig[slot].count++;
        break;
    }
    if (i == ER_NSIG) er.sig_lost++;

    if (now - er.last_dump >= 10 * (ULONGLONG)1000000000)
    {
        er.last_dump = now;
        er_dump();
    }
}

/* NtClose() on any thread, when the log is on */
void er_note_close( HANDLE handle )
{
    ULONG i = ((ULONG_PTR)handle >> 2) & (ER_NHANDLE - 1);
    ULONGLONG life;

    if (NtCurrentTeb() != er_main_teb) return;
    if (!handle || er_live[i].handle != handle)
    {
        er.closes_other++;
        return;
    }
    er_live[i].handle = 0;
    er.closes_own++;
    life = er_now_ns() - er_live[i].time;
    er.life[life < 10000 ? 0 : life < 100000 ? 1 : life < 1000000 ? 2 : life < 16700000 ? 3 :
            life < 100000000 ? 4 : life < 1000000000 ? 5 : 6]++;
}

/* wait_reply() on the main thread: poll for the reply before the blocking read */
void er_spin_for_reply( int fd )
{
    struct pollfd pfd;
    ULONGLONG start = er_now_ns(), now;

    pfd.fd = fd;
    pfd.events = POLLIN;
    for (;;)
    {
        pfd.revents = 0;
        if (poll( &pfd, 1, 0 ) > 0)
        {
            er_note_spin( 0, TRUE, er_now_ns() - start );
            return;
        }
        now = er_now_ns();
        if (now - start >= er_server_spin_ns)
        {
            er_note_spin( 0, FALSE, now - start );
            return;
        }
    }
}

/* NtWaitForAlertByThreadId() on the main thread: wait for the alert flag in user space first */
static void er_spin_on_futex( LONG *futex )
{
    ULONGLONG start = er_now_ns(), now;
    unsigned int i = 0;

    while (!ReadNoFence( futex ))
    {
        YieldProcessor();
        if (++i % 32) continue;
        now = er_now_ns();
        if (now - start >= er_sync_spin_ns)
        {
            er_note_spin( 1, FALSE, now - start );
            return;
        }
    }
    er_note_spin( 1, TRUE, er_now_ns() - start );
}
'''

NTSYNC_OLD = '''    if (type != WaitAll || count == 1) request = NTSYNC_IOC_WAIT_ANY;
    else request = NTSYNC_IOC_WAIT_ALL;

    do { ret = ioctl( device, request, &args ); }
    while (ret < 0 && errno == EINTR);

    if (!ret)
    {
        if (args.index == count)'''
NTSYNC_NEW = '''    if (type != WaitAll || count == 1) request = NTSYNC_IOC_WAIT_ANY;
    else request = NTSYNC_IOC_WAIT_ALL;

    /* er-wine: on the main thread, poll with an already expired timeout before blocking */
    if (er_sync_spin_ns && NtCurrentTeb() == er_main_teb &&
        (!timeout || timeout->QuadPart == TIMEOUT_INFINITE ||
         (timeout->QuadPart < 0 && (ULONGLONG)-timeout->QuadPart * 100 > 2 * er_sync_spin_ns)))
    {
        struct ntsync_wait_args spin = args;
        ULONGLONG start = er_now_ns(), now = start;

        spin.timeout = 0;
        for (;;)
        {
            spin.index = ~0u;
            ret = ioctl( device, request, &spin );
            if (!ret || (errno != ETIMEDOUT && errno != EINTR)) break;
            now = er_now_ns();
            if (now - start >= er_sync_spin_ns) break;
            YieldProcessor();
        }
        if (!ret || (errno != ETIMEDOUT && errno != EINTR))
        {
            int err = errno;

            er_note_spin( 2, TRUE, er_now_ns() - start );
            args.index = spin.index;
            errno = err;
            goto done;
        }
        er_note_spin( 2, FALSE, now - start );
    }

    do { ret = ioctl( device, request, &args ); }
    while (ret < 0 && errno == EINTR);

done:
    if (!ret)
    {
        if (args.index == count)'''

CREATE_OLD = '''        req->manual_reset = (type == NotificationEvent);
        req->initial_state = state;
        wine_server_add_data( req, objattr, len );
        ret = wine_server_call( req );
        *handle = wine_server_ptr_handle( reply->handle );
    }
    SERVER_END_REQ;

    free( objattr );
    return ret;'''
CREATE_NEW = '''        req->manual_reset = (type == NotificationEvent);
        req->initial_state = state;
        wine_server_add_data( req, objattr, len );
        ret = wine_server_call( req );
        *handle = wine_server_ptr_handle( reply->handle );
    }
    SERVER_END_REQ;

    free( objattr );
    if (er_log_on && !ret) er_note_event_create( attr, type, state, *handle );
    return ret;'''

FUTEX_OLD = '''        while (!InterlockedExchange( futex, 0 ))
'''
FUTEX_NEW = '''        if (er_sync_spin_ns && NtCurrentTeb() == er_main_teb && !ReadNoFence( futex ) &&
            (!timeout || (ULONGLONG)update_timeout( end ) * 100 > 2 * er_sync_spin_ns))
            er_spin_on_futex( futex );

        while (!InterlockedExchange( futex, 0 ))
'''

EDITS = {
    U + "sync.c": [
        ("int inproc_device_fd = -1;\n", "int inproc_device_fd = -1;\n" + ER_BLOCK),
        (NTSYNC_OLD, NTSYNC_NEW),
        (CREATE_OLD, CREATE_NEW),
        (FUTEX_OLD, FUTEX_NEW),
    ],
    U + "server.c": [
        ('''static inline unsigned int wait_reply( struct __server_request_info *req )
{
    read_reply_data( &req->u.reply, sizeof(req->u.reply) );''',
         '''static inline unsigned int wait_reply( struct __server_request_info *req )
{
    if (er_server_spin_ns && NtCurrentTeb() == er_main_teb)
        er_spin_for_reply( ntdll_get_thread_data()->reply_fd );
    read_reply_data( &req->u.reply, sizeof(req->u.reply) );'''),
        ('''    if (HandleToLong( handle ) >= ~5 && HandleToLong( handle ) <= ~0)
        return STATUS_SUCCESS;

    /* hold fd_cache_mutex''',
         '''    if (HandleToLong( handle ) >= ~5 && HandleToLong( handle ) <= ~0)
        return STATUS_SUCCESS;

    if (er_log_on) er_note_close( handle );

    /* hold fd_cache_mutex'''),
    ],
    U + "loader.c": [
        ('''    const char *sgi = getenv( "SteamGameId" );
    const char *env_str;
''',
         '''    const char *sgi = getenv( "SteamGameId" );
    const char *env_str;

    er_init( main_argc > 1 ? main_argv[1] : NULL );
'''),
    ],
    U + "unix_private.h": [
        ('''extern BOOL virtual_is_valid_code_address( const void *addr, SIZE_T size );
''',
         '''extern BOOL virtual_is_valid_code_address( const void *addr, SIZE_T size );
extern BOOL virtual_er_is_exec( const void *addr );

/* er-wine (Elden Ring tuning), see sync.c */
extern TEB *er_main_teb;
extern ULONGLONG er_server_spin_ns;
extern ULONGLONG er_sync_spin_ns;
extern BOOL er_log_on;
extern void er_init( const char *exe );
extern void er_spin_for_reply( int fd );
extern void er_note_close( HANDLE handle );
extern ULONG er_get_pe_callers( ULONG_PTR *addrs, ULONG max );
'''),
    ],
    U + "virtual.c": [
        ('''    if ((view = find_view( addr, size )))
        ret = !(view->protect & VPROT_SYSTEM);  /* system views are not visible to the app */
    server_leave_uninterrupted_section( &virtual_mutex, &sigset );
    return ret;
}
''',
         '''    if ((view = find_view( addr, size )))
        ret = !(view->protect & VPROT_SYSTEM);  /* system views are not visible to the app */
    server_leave_uninterrupted_section( &virtual_mutex, &sigset );
    return ret;
}


/***********************************************************************
 *           virtual_er_is_exec
 *
 * er-wine: whether addr is in executable memory. No lock: only used for statistics.
 */
BOOL virtual_er_is_exec( const void *addr )
{
    return (get_page_vprot( addr ) & VPROT_EXEC) != 0;
}
'''),
    ],
    U + "signal_x86_64.c": [
        ('''void set_thread_teb( TEB *teb )
{
    arch_prctl( ARCH_SET_GS, teb );
}
''',
         '''void set_thread_teb( TEB *teb )
{
    arch_prctl( ARCH_SET_GS, teb );
}

/***********************************************************************
 *           er_get_pe_callers
 *
 * er-wine: up to max code addresses on the PE stack of the current syscall,
 * innermost first (the caller of the Nt function, then its callers).
 */
ULONG er_get_pe_callers( ULONG_PTR *addrs, ULONG max )
{
    struct syscall_frame *frame = get_syscall_frame();
    NT_TIB *tib = &NtCurrentTeb()->Tib;
    ULONG_PTR *sp, *end;
    ULONG n = 0;

    if (!frame) return 0;
    sp = (ULONG_PTR *)frame->rsp;
    if ((void *)sp < tib->StackLimit || (void *)sp >= tib->StackBase) return 0;
    end = sp + 128;
    if ((void *)end > tib->StackBase) end = (ULONG_PTR *)tib->StackBase;
    for (; sp < end && n < max; sp++)
        if (*sp >= 0x10000 && virtual_er_is_exec( (const void *)*sp )) addrs[n++] = *sp;
    return n;
}
'''),
    ],
}

new_text = {}
for path, edits in EDITS.items():
    text = open(path).read()
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            sys.exit(f"anchor matched {n} times in {path}:\n{old[:200]}")
        text = text.replace(old, new)
    new_text[path] = text
for path, text in new_text.items():
    with open(path, "w") as f:
        f.write(text)
print("patched:", ", ".join(p.rsplit("/", 1)[-1] for p in new_text))
