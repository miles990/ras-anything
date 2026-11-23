package _ras_agent;

import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import java.util.Map;

/** Which fault hits the n-th attempt of a request key: the same pure function as faults.py. */
final class Plan {
    static final String[] KINDS = {"timeout", "refused", "http5xx", "http429", "slow", "truncated"};
    static final double SLOW_DEFAULT = 3.0;
    static int outageK = 3;

    final String type, fault, key, seed, salt;
    final double rate, outageRate;
    final List<?> faults;
    final boolean repeat;

    Plan(Map<?, ?> m) {
        type = m == null ? "none" : String.valueOf(m.get("type"));
        Map<?, ?> p = m == null ? Map.of() : m;
        fault = (String) p.get("fault");
        key = (String) p.get("key");
        seed = p.get("seed") == null ? "None" : String.valueOf(p.get("seed"));
        salt = p.get("salt") == null ? "" : String.valueOf(p.get("salt"));
        rate = p.get("rate") instanceof Number n ? n.doubleValue() : 0;
        outageRate = p.get("outage_rate") instanceof Number n ? n.doubleValue() : 0;
        faults = p.get("faults") instanceof List<?> l ? l : List.of();
        repeat = !(Boolean.FALSE.equals(p.get("repeat")));
    }

    static Plan none() {
        return new Plan(null);
    }

    static double u(Object... parts) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < parts.length; i++) b.append(i == 0 ? "" : "|").append(parts[i]);
        byte[] h = sha256(b.toString().getBytes(StandardCharsets.UTF_8));
        return new BigInteger(1, java.util.Arrays.copyOf(h, 8)).doubleValue() / 18446744073709551616.0;
    }

    static byte[] sha256(byte[] data) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(data);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    String fault(String k, int n) {
        switch (type) {
            case "down":
                return "refused";
            case "first":
                if (key != null && !key.equals(k)) return null;
                if ("outage".equals(fault)) return n < outageK ? "refused" : null;
                return n == 0 ? fault : null;
            case "schedule":
                if (faults.isEmpty() || (key != null && !key.equals(k))) return null;
                if (n < faults.size()) return (String) faults.get(n);
                return repeat ? (String) faults.get(faults.size() - 1) : null;
            case "seeded":
                if (outage(k, n)) return "refused";
                if (u(seed, salt, k, n, "p") < rate) return KINDS[(int) (u(seed, salt, k, n, "k") * KINDS.length) % KINDS.length];
                return null;
            default:
                return null;
        }
    }

    boolean outage(String k, int n) {
        return switch (type) {
            case "down" -> true;
            case "first" -> "outage".equals(fault) && n < outageK && (key == null || key.equals(k));
            case "seeded" -> outageRate > 0 && n < outageK && u(seed, salt, k, "outage") < outageRate;
            default -> false;
        };
    }
}
