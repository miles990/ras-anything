package _ras_agent;

/** A call with no timeout would have blocked forever. Reported even if the app swallows it. */
public final class ChaosHang extends Error {
    ChaosHang(String where) {
        super("boundary call with no timeout hung (chaos): " + where);
    }
}
