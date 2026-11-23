// Hardened shipping.Labels.labelCode, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.
package shipping;

import java.io.IOException;
import java.time.Duration;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import shipping._ras_hardened.RasRt;

// Compiled as a class inside Labels: its private members and nested types are in scope by their plain names.
final class Hardened {
    // No RAS_FALLBACK: a made-up label code would be printed on a parcel, so a give-up is a structured error.
    private static final String TOOL = "label-tool";

    // Computing a label changes nothing, so it can be repeated. A slow run (3 s) fits in one attempt, and a hung one leaves time for a second.
    private static final RasRt.Policy LABELS = RasRt.policy(TOOL).deadline(Duration.ofMillis(4500)).attemptTimeout(Duration.ofMillis(3300)).attempts(4);

    // "<checksum> <bytes>\n": the newline comes last, so output without it was cut off.
    private static final Pattern OUTPUT = Pattern.compile("(\\d+) \\d+\\R");

    static String labelCode(String tracking) throws IOException, InterruptedException {
        try {
            return LABELS.call(a -> codeIn(RasRt.run(a, "sh", "tools/label.sh", tracking)));
        } catch (RasRt.RasError e) {
            // Already logged by the policy. Callers know this function's failures as IOException.
            throw new RasRt.RasIOException(e.getMessage(), e.getCause(), TOOL);
        } catch (IOException | InterruptedException | RuntimeException e) {
            throw e;
        } catch (Exception e) { // Policy.call declares it; nothing above throws another checked exception
            throw new IOException(e);
        }
    }

    private static String codeIn(RasRt.Result r) throws IOException {
        if (r.code != 0) throw new IOException("label tool exited " + r.code);
        Matcher m = OUTPUT.matcher(r.stdout);
        if (!m.matches()) throw new RasRt.BadBodyException("label tool output is incomplete");
        return m.group(1);
    }
}
