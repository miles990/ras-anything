package _ras_agent;

/** The replayed code made a request the recording doesn't have. Reported even if the app swallows it. */
public final class ReplayMiss extends Error {
    ReplayMiss(String what) {
        super(what);
    }
}
