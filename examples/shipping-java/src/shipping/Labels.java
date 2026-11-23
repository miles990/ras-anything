package shipping;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** Label codes come from the warehouse's label tool, which prints "<code> <length of the tracking number>". */
final class Labels {
    private Labels() {}

    static String labelCode(String tracking) throws IOException, InterruptedException {
        Process p = new ProcessBuilder("sh", "tools/label.sh", tracking).start();
        String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        if (p.waitFor() != 0) throw new IOException("label tool exited " + p.exitValue());
        return out.split(" ")[0];
    }
}
