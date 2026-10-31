"""er-wine patch 2 (event pool + deferred close on the main thread), applied as text
edits on a Wine tree that already has 0001-er-main-thread-spin.patch applied.
The result, as `git diff`, is 0002-er-event-pool.patch (see RESEARCH.md).
usage: python3 -I apply-er-pool.py <wine source root>"""
import os, sys

root = sys.argv[1]
unix = os.path.join(root, "dlls/ntdll/unix")
edits = []  # (file, old, new): old must occur exactly once


def edit(f, old, new):
    edits.append((f, old, new))


# --- sync.c ------------------------------------------------------------------

edit("sync.c", """#ifdef HAVE_SYS_RESOURCE_H
# include <sys/resource.h>
#endif
""", """#ifdef HAVE_SYS_RESOURCE_H
# include <sys/resource.h>
#endif
#ifdef HAVE_SYS_PRCTL_H
# include <sys/prctl.h>
#endif
""")

edit("sync.c", """ *   WINE_ER_EXE=<name>     only in the process whose exe path ends with <name>
""", """ *   WINE_ER_EXE=<name>     only in the process whose exe path ends with <name>
 *   WINE_ER_POOL=N         event pool: up to N fresh events of each initial state
 *                          ready for the main thread, see er_pool_take()
""")

edit("sync.c", """BOOL er_log_on;
""", """BOOL er_log_on;
BOOL er_pool_on;
""")

edit("sync.c", """    ULONG closes_own, closes_other, life[7];
} er;
""", """    ULONG closes_own, closes_other, life[7];
    ULONG pool_hits[2], pool_untracked, pool_empty, pool_skip, pool_deferred, pool_close_full, pool_kicks;
} er;
""")

edit("sync.c", """static struct { HANDLE handle; ULONGLONG time; } er_live[ER_NHANDLE];
""", """static struct { HANDLE handle; ULONGLONG time; } er_live[ER_NHANDLE];

/* event pool (WINE_ER_POOL), see er_pool_take() */
#define ER_RING  1024  /* ring buffer entries (a power of 2); WINE_ER_POOL is at most half */
/* pooled events the main thread holds, by handle index; big enough to never be shared.
 * With 4096 slots shared by index, only 31% of the main thread's closes were deferred
 * (2026-10-31 runs): events the game keeps open for long likely held the rest. */
#define ER_SLOTS (1 << 18)
struct er_ring
{
    LONG h[ER_RING];                  /* wine_server_obj_handle() values */
    LONG DECLSPEC_ALIGN(64) head;     /* next entry to take, written by the consumer only */
    LONG DECLSPEC_ALIGN(64) tail;     /* next free entry, written by the producer only */
};
static ULONG er_pool_size;
static int er_pool_started;           /* 1 = helper thread created, -1 = creating it failed */
static TEB *er_pool_teb;              /* the helper thread, once it runs */
static LONG er_pool_wake;             /* futex: 1 = the helper has work */
static ULONG er_pool_made, er_pool_closed;  /* written by the helper only */
static struct er_ring er_pool[2];     /* helper -> main thread: fresh manual-reset events, by initial state */
static struct er_ring er_closeq;      /* main thread -> helper: pooled events to close */
/* per pooled event the main thread holds: its handle, or handle | 1 once its close is queued */
static LONG er_slot[ER_SLOTS];
""")

edit("sync.c", """    const char *want = getenv( "WINE_ER_EXE" ), *log = getenv( "WINE_ER_LOG" );

    if (want && *want && (!exe || !er_ends_with( exe, want ))) return;
    er_server_spin_ns = er_env_us( "WINE_SERVER_SPIN_US" );
    er_sync_spin_ns = er_env_us( "WINE_SYNC_SPIN_US" );
    if (log && *log) er_log_path = strdup( log );
    if (!er_server_spin_ns && !er_sync_spin_ns && !er_log_path) return;
    er.start = er.last_dump = er_now_ns();
    er_main_teb = NtCurrentTeb();
    er_log_on = er_log_path != NULL;
    ERR( "HACK: er-wine: main thread spins: server reply %u us, sync %u us; log %s\\n",
         (unsigned int)(er_server_spin_ns / 1000), (unsigned int)(er_sync_spin_ns / 1000),
         er_log_path ? er_log_path : "off" );
""", """    const char *want = getenv( "WINE_ER_EXE" ), *log = getenv( "WINE_ER_LOG" ), *pool = getenv( "WINE_ER_POOL" );

    if (want && *want && (!exe || !er_ends_with( exe, want ))) return;
    er_server_spin_ns = er_env_us( "WINE_SERVER_SPIN_US" );
    er_sync_spin_ns = er_env_us( "WINE_SYNC_SPIN_US" );
    if (log && *log) er_log_path = strdup( log );
    er_pool_size = pool ? strtoul( pool, NULL, 10 ) : 0;
    if (er_pool_size > ER_RING / 2) er_pool_size = ER_RING / 2;
    if (!er_server_spin_ns && !er_sync_spin_ns && !er_log_path && !er_pool_size) return;
    er.start = er.last_dump = er_now_ns();
    er_main_teb = NtCurrentTeb();
    er_log_on = er_log_path != NULL;
    er_pool_on = er_pool_size != 0;
    ERR( "HACK: er-wine: main thread spins: server reply %u us, sync %u us; event pool %u; log %s\\n",
         (unsigned int)(er_server_spin_ns / 1000), (unsigned int)(er_sync_spin_ns / 1000),
         (unsigned int)er_pool_size, er_log_path ? er_log_path : "off" );
""")

edit("sync.c", """    er_append( buf, sizeof(buf), &len, "NtCreateEvent: main thread %u (%llu/s), other threads %d\\n",
""", """    if (er_pool_size)
        er_append( buf, sizeof(buf), &len, "event pool: %u per state, helper %s; served %u unsignaled + %u signaled "
                   "(untracked %u), pool empty %u, not eligible %u; closes deferred %u, queue full %u; "
                   "helper made %u, closed %u; wakeups %u\\n", (unsigned int)er_pool_size,
                   er_pool_started < 0 ? "failed to start" : er_pool_teb ? "running" : "not started",
                   (unsigned int)er.pool_hits[0], (unsigned int)er.pool_hits[1], (unsigned int)er.pool_untracked,
                   (unsigned int)er.pool_empty,
                   (unsigned int)er.pool_skip, (unsigned int)er.pool_deferred, (unsigned int)er.pool_close_full,
                   (unsigned int)er_pool_made, (unsigned int)er_pool_closed, (unsigned int)er.pool_kicks );
    er_append( buf, sizeof(buf), &len, "NtCreateEvent: main thread %u (%llu/s), other threads %d\\n",
""")

edit("sync.c", """    if (NtCurrentTeb() != er_main_teb)
    {
        InterlockedIncrement( &er.creates_other );
        return;
    }
""", """    if (NtCurrentTeb() != er_main_teb)
    {
        if (NtCurrentTeb() != er_pool_teb) InterlockedIncrement( &er.creates_other );
        return;
    }
""")

# The pool code goes after the futex helpers (it uses them), right before NtCreateEvent.
edit("sync.c", """/**************************************************************************
 *              NtCreateEvent (NTDLL.@)
 */
""", """/***********************************************************************
 * er-wine: event pool and deferred close on the main thread (WINE_ER_POOL=N).
 *
 * Nearly all events the game's main thread creates are unnamed manual-reset
 * ones that it closes again within a frame: ~3.7K creates and ~3.1K closes a
 * second, each a wineserver round trip, ~0.6 ms per frame together. A system
 * thread (Unix side only, like the threads of Wine's audio drivers) keeps up
 * to N fresh events of each initial state ready and closes the main thread's
 * pooled events for it. Every pooled handle is a new event straight from the
 * server: nothing is reused, closes just happen a little later.
 */

static inline LONG *er_slot_of( LONG h )
{
    return &er_slot[((ULONG)h >> 2) & (ER_SLOTS - 1)];
}

static inline ULONG er_ring_count( struct er_ring *ring )
{
    return (ULONG)ReadAcquire( &ring->tail ) - (ULONG)ReadAcquire( &ring->head );
}

/* producer side; the caller checks that there is room */
static void er_ring_put( struct er_ring *ring, LONG h )
{
    ULONG tail = ring->tail;

    ring->h[tail % ER_RING] = h;
    WriteRelease( &ring->tail, (LONG)(tail + 1) );
}

/* consumer side: 0 if the ring is empty */
static LONG er_ring_get( struct er_ring *ring )
{
    ULONG head = ring->head;
    LONG h;

    if (head == (ULONG)ReadAcquire( &ring->tail )) return 0;
    h = ring->h[head % ER_RING];
    WriteRelease( &ring->head, (LONG)(head + 1) );
    return h;
}

/* main thread: wake the helper thread, at most once per pass of it */
static void er_pool_kick(void)
{
    if (ReadNoFence( &er_pool_wake ) || InterlockedExchange( &er_pool_wake, 1 )) return;
    er.pool_kicks++;
    futex_wake_one( &er_pool_wake );
}

static DWORD WINAPI er_pool_thread( void *arg )
{
    struct timespec timeout = { 0, 4000000 };  /* a pass at least every 4 ms */
    HANDLE handle;
    ULONG state;
    LONG h;

    er_pool_teb = NtCurrentTeb();
#ifdef PR_SET_NAME
    prctl( PR_SET_NAME, "wine_er_pool" );
#endif
    for (;;)
    {
        WriteRelease( &er_pool_wake, 0 );
        /* closes first, they free handle slots */
        while ((h = er_ring_get( &er_closeq )))
        {
            InterlockedCompareExchange( er_slot_of( h ), 0, h | 1 );
            NtClose( wine_server_ptr_handle( h ) );
            er_pool_closed++;
        }
        for (state = 0; state < 2; state++)
        {
            while (er_ring_count( &er_pool[state] ) < er_pool_size &&
                   !NtCreateEvent( &handle, EVENT_ALL_ACCESS, NULL, NotificationEvent, state ))
            {
                er_ring_put( &er_pool[state], wine_server_obj_handle( handle ) );
                er_pool_made++;
            }
        }
        futex_wait( &er_pool_wake, 0, &timeout );
    }
    return 0;
}

/* NtCreateEvent() on the main thread: a fresh event from the pool, or 0 to create one as usual */
static HANDLE er_pool_take( ACCESS_MASK access, const OBJECT_ATTRIBUTES *attr, EVENT_TYPE type, BOOLEAN state )
{
    static ULONG calls;
    struct er_ring *pool = &er_pool[state ? 1 : 0];
    HANDLE thread;
    LONG h;

    /* only what an unnamed CreateEvent() asks for; a handle with EVENT_ALL_ACCESS covers it */
    if (type != NotificationEvent || !access || (access & ~EVENT_ALL_ACCESS) ||
        (attr && (attr->Length != sizeof(*attr) || attr->RootDirectory || attr->ObjectName ||
                  attr->SecurityDescriptor || (attr->Attributes & ~(OBJ_CASE_INSENSITIVE | OBJ_OPENIF)))))
    {
        er.pool_skip++;
        return 0;
    }
    if (er_pool_started <= 0)
    {
        /* start the helper once the game is past its first events */
        if (!er_pool_started && ++calls >= 1000)
        {
            if (PsCreateSystemThread( &thread, THREAD_ALL_ACCESS, NULL, 0, NULL, er_pool_thread, NULL ))
            {
                er_pool_started = -1;
                er_pool_on = FALSE;
            }
            else
            {
                er_pool_started = 1;
                NtClose( thread );
            }
        }
        return 0;
    }
    if (!(h = er_ring_get( pool )))
    {
        er.pool_empty++;
        er_pool_kick();
        return 0;
    }
    if (er_ring_count( pool ) <= er_pool_size / 2) er_pool_kick();
    /* untracked (slot taken): still a fine event, it just gets closed the usual way */
    if (InterlockedCompareExchange( er_slot_of( h ), h, 0 )) er.pool_untracked++;
    er.pool_hits[state ? 1 : 0]++;
    return wine_server_ptr_handle( h );
}

/* NtClose() on any thread while the pool is on: TRUE if the close was handled here */
BOOL er_pool_close( HANDLE handle, unsigned int *status )
{
    LONG h = wine_server_obj_handle( handle ) & ~3, *slot = er_slot_of( h ), v;
    TEB *teb = NtCurrentTeb();

    if (!h || teb == er_pool_teb) return FALSE;
    for (;;)
    {
        v = ReadAcquire( slot );
        if (v == (h | 1))  /* already closed on the main thread, the close is queued */
        {
            *status = STATUS_INVALID_HANDLE;
            return TRUE;
        }
        if (v != h) return FALSE;  /* not a pooled event */
        if (teb != er_main_teb || er_ring_count( &er_closeq ) >= ER_RING)
        {
            /* closed on another thread, or the queue is full: close it now, as usual */
            if (InterlockedCompareExchange( slot, 0, h ) != h) continue;
            if (teb == er_main_teb) er.pool_close_full++;
            return FALSE;
        }
        if (InterlockedCompareExchange( slot, h | 1, h ) != h) continue;
        er_ring_put( &er_closeq, h );
        if (er_ring_count( &er_closeq ) >= 32) er_pool_kick();
        er.pool_deferred++;
        *status = STATUS_SUCCESS;
        return TRUE;
    }
}

/* NtDuplicateObject( DUPLICATE_CLOSE_SOURCE ) while the pool is on: FALSE if the
 * handle was already closed on the main thread (its close is queued) */
BOOL er_pool_forget( HANDLE handle )
{
    LONG h = wine_server_obj_handle( handle ) & ~3, *slot = er_slot_of( h ), v;

    if (!h || NtCurrentTeb() == er_pool_teb) return TRUE;
    for (;;)
    {
        v = ReadAcquire( slot );
        if (v == (h | 1)) return FALSE;
        if (v != h || InterlockedCompareExchange( slot, 0, h ) == h) return TRUE;
    }
}


/**************************************************************************
 *              NtCreateEvent (NTDLL.@)
 */
""")

edit("sync.c", """    *handle = 0;
    if (type != NotificationEvent && type != SynchronizationEvent) return STATUS_INVALID_PARAMETER;
    if ((ret = alloc_object_attributes( attr, &objattr, &len ))) return ret;

    SERVER_START_REQ( create_event )
""", """    *handle = 0;
    if (type != NotificationEvent && type != SynchronizationEvent) return STATUS_INVALID_PARAMETER;
    if (er_pool_on && NtCurrentTeb() == er_main_teb && (*handle = er_pool_take( access, attr, type, state )))
    {
        if (er_log_on) er_note_event_create( attr, type, state, *handle );
        return STATUS_SUCCESS;
    }
    if ((ret = alloc_object_attributes( attr, &objattr, &len ))) return ret;

    SERVER_START_REQ( create_event )
""")

# --- server.c ------------------------------------------------------------------

edit("server.c", """        return result.dup_handle.status;
    }

    /* hold fd_cache_mutex""", """        return result.dup_handle.status;
    }

    if ((options & DUPLICATE_CLOSE_SOURCE) && er_pool_on && !er_pool_forget( source ))
        return STATUS_INVALID_HANDLE;

    /* hold fd_cache_mutex""")

edit("server.c", """    if (er_log_on) er_note_close( handle );
""", """    if (er_log_on) er_note_close( handle );
    if (er_pool_on && er_pool_close( handle, &ret )) return ret;
""")

# --- unix_private.h ------------------------------------------------------------

edit("unix_private.h", """extern BOOL er_log_on;
""", """extern BOOL er_log_on;
extern BOOL er_pool_on;
extern BOOL er_pool_close( HANDLE handle, unsigned int *status );
extern BOOL er_pool_forget( HANDLE handle );
""")

texts = {}
for f, old, new in edits:
    path = os.path.join(unix, f)
    if f not in texts:
        texts[f] = open(path).read()
    n = texts[f].count(old)
    if n != 1:
        sys.exit(f"{f}: expected 1 match, found {n}, for:\n{old}")
    texts[f] = texts[f].replace(old, new)
for f, t in texts.items():
    open(os.path.join(unix, f), "w").write(t)
print(f"applied {len(edits)} edits to {', '.join(texts)}")
