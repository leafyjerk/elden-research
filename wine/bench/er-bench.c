/* er-bench: times, on the main thread, what er-wine's patches target (see PATCH-LOG.md):
 *   manual   manual-reset CreateEvent and CloseHandle in bursts of 32, 1 ms apart
 *            (the game's pattern; served from the event pool with WINE_ER_POOL)
 *   check    pooled events must behave like fresh ones (initial state, set, reset, close)
 *   create   auto-reset CreateEvent + CloseHandle: two wineserver round trips (never pooled)
 *   event    SetEvent to a worker thread, then wait for its SetEvent back (ntsync waits)
 *   address  RtlWaitOnAddress ping-pong with a worker thread (NtWaitForAlertByThreadId)
 * Then it idles until 10.5 s after start and creates one more event, so a WINE_ER_LOG
 * gets written (er-wine dumps it from NtCreateEvent, every 10 s; the phases after
 * "create" make no events, so that first dump has everything).
 * No CRT and no Windows headers; run-bench.sh builds it with clang. */

typedef void *HANDLE;
typedef unsigned int DWORD;
typedef long long LONGLONG;
#define INFINITE 0xffffffffu
#define WAIT_OBJECT_0 0
#define WAIT_TIMEOUT 258
#define WINAPI __stdcall
#define IMPORT __declspec(dllimport)

IMPORT HANDLE WINAPI CreateEventW( void *sa, int manual, int initial, const unsigned short *name );
IMPORT int WINAPI SetEvent( HANDLE event );
IMPORT int WINAPI ResetEvent( HANDLE event );
IMPORT DWORD WINAPI WaitForSingleObject( HANDLE handle, DWORD ms );
IMPORT int WINAPI CloseHandle( HANDLE handle );
IMPORT HANDLE WINAPI CreateThread( void *sa, unsigned long long stack, DWORD (WINAPI *fn)(void *), void *arg,
                                   DWORD flags, DWORD *tid );
IMPORT int WINAPI QueryPerformanceCounter( LONGLONG *count );
IMPORT int WINAPI QueryPerformanceFrequency( LONGLONG *freq );
IMPORT void WINAPI Sleep( DWORD ms );
IMPORT long WINAPI RtlWaitOnAddress( const volatile void *addr, const void *cmp, unsigned long long size,
                                     const LONGLONG *timeout );
IMPORT void WINAPI RtlWakeAddressSingle( const volatile void *addr );
IMPORT int __cdecl printf( const char *fmt, ... );
IMPORT void __cdecl exit( int code );

#define PHASE_NS 3000000000LL

static LONGLONG freq;
static HANDLE go, back;
static volatile long turn, stop;

static LONGLONG now_ns(void)
{
    LONGLONG t;

    QueryPerformanceCounter( &t );
    return t / freq * 1000000000 + t % freq * 1000000000 / freq;
}

static void report( const char *name, LONGLONG n, LONGLONG ns )
{
    printf( "%-8s %7d per s  %6d ns each\n", name, (int)(n * 1000000000 / ns), (int)(ns / n) );
}

static DWORD WINAPI event_worker( void *arg )
{
    for (;;)
    {
        WaitForSingleObject( go, INFINITE );
        if (stop) return 0;
        SetEvent( back );
    }
}

static DWORD WINAPI address_worker( void *arg )
{
    long zero = 0;

    for (;;)
    {
        while (!turn) RtlWaitOnAddress( &turn, &zero, sizeof(turn), 0 );
        if (stop) return 0;
        turn = 0;
        RtlWakeAddressSingle( &turn );
    }
}

void start(void)
{
    LONGLONG t0, s, t, n, left, tc, tx;
    HANDLE worker, h[32], e;
    long one = 1;
    int i, bad;

    QueryPerformanceFrequency( &freq );
    t0 = now_ns();

    for (n = tc = tx = 0, s = now_ns(); now_ns() - s < PHASE_NS; n += 32)
    {
        t = now_ns();
        for (i = 0; i < 32; i++) h[i] = CreateEventW( 0, 1, i & 1, 0 );
        tc += now_ns() - t;
        t = now_ns();
        for (i = 0; i < 32; i++) CloseHandle( h[i] );
        tx += now_ns() - t;
        Sleep( 1 );
    }
    printf( "manual   create %5d ns, close %5d ns each (%d in bursts of 32, 1 ms apart)\n",
            (int)(tc / n), (int)(tx / n), (int)n );

    for (i = bad = 0; i < 2000; i++)
    {
        if (!(e = CreateEventW( 0, 1, i & 1, 0 )))
        {
            bad++;
            continue;
        }
        if (WaitForSingleObject( e, 0 ) != (i & 1 ? WAIT_OBJECT_0 : WAIT_TIMEOUT)) bad++;
        if (!SetEvent( e ) || WaitForSingleObject( e, 0 ) != WAIT_OBJECT_0) bad++;
        if (!ResetEvent( e ) || WaitForSingleObject( e, 0 ) != WAIT_TIMEOUT) bad++;
        if (!CloseHandle( e )) bad++;
    }
    printf( "check    %d of 2000 events misbehaved\n", bad );

    for (n = 0, s = now_ns(); (t = now_ns()) - s < PHASE_NS; n += 16)
        for (i = 0; i < 16; i++) CloseHandle( CreateEventW( 0, 0, 0, 0 ) );
    report( "create", n, t - s );

    go = CreateEventW( 0, 0, 0, 0 );
    back = CreateEventW( 0, 0, 0, 0 );
    worker = CreateThread( 0, 0, event_worker, 0, 0, 0 );
    for (n = 0, s = now_ns(); (t = now_ns()) - s < PHASE_NS; n += 16)
        for (i = 0; i < 16; i++)
        {
            SetEvent( go );
            WaitForSingleObject( back, INFINITE );
        }
    report( "event", n, t - s );
    stop = 1;
    SetEvent( go );
    WaitForSingleObject( worker, INFINITE );
    CloseHandle( worker );
    stop = 0;

    worker = CreateThread( 0, 0, address_worker, 0, 0, 0 );
    for (n = 0, s = now_ns(); (t = now_ns()) - s < PHASE_NS; n += 16)
        for (i = 0; i < 16; i++)
        {
            turn = 1;
            RtlWakeAddressSingle( &turn );
            while (turn) RtlWaitOnAddress( &turn, &one, sizeof(turn), 0 );
        }
    report( "address", n, t - s );
    stop = 1;
    turn = 1;
    RtlWakeAddressSingle( &turn );
    WaitForSingleObject( worker, INFINITE );

    if ((left = t0 + 10500000000LL - now_ns()) > 0) Sleep( (DWORD)(left / 1000000) );
    CloseHandle( CreateEventW( 0, 0, 0, 0 ) );
    exit( 0 );
}
