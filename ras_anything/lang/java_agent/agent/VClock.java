package _ras_agent;

import java.util.PriorityQueue;

/** Virtual time (nanoseconds) with events; sleeping costs no real time. */
final class VClock {
    record Event(long at, long seq, Runnable run) {}

    private final PriorityQueue<Event> queue = new PriorityQueue<>((a, b) -> a.at != b.at ? Long.compare(a.at, b.at) : Long.compare(a.seq, b.seq));
    private long now, seq;

    synchronized long now() {
        return now;
    }

    synchronized void clear() {
        queue.clear();
    }

    synchronized Event schedule(long at, Runnable run) {
        Event e = new Event(Math.max(at, now), seq++, run);
        queue.add(e);
        return e;
    }

    synchronized void cancel(Event e) {
        queue.remove(e);
    }

    /** Moves time to `t`, running what is due on the way. */
    void advanceTo(long t) {
        while (pump(t)) { }
        synchronized (this) {
            now = Math.max(now, t);
        }
    }

    /** Runs the next event if it is due by `limit`. */
    boolean pump(long limit) {
        Event e;
        synchronized (this) {
            e = queue.peek();
            if (e == null || e.at > limit) return false;
            queue.poll();
            now = Math.max(now, e.at);
        }
        e.run.run();
        return true;
    }
}
