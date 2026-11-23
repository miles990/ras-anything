package _ras_agent;

import com.sun.source.tree.*;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TaskEvent;
import com.sun.source.util.TaskListener;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.TreeScanner;
import com.sun.source.util.Trees;
import java.io.IOException;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.ExecutableElement;
import javax.lang.model.element.Modifier;
import javax.lang.model.element.RecordComponentElement;
import javax.lang.model.element.TypeElement;
import javax.lang.model.type.ArrayType;
import javax.lang.model.type.DeclaredType;
import javax.lang.model.type.TypeKind;
import javax.lang.model.type.TypeMirror;
import javax.tools.Diagnostic;
import javax.tools.DiagnosticCollector;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

/**
 * Reads Java with the JDK's own compiler: lists functions and their reliability signals, instruments a copy of the
 * sources, and takes hardened files apart. One JSON request on stdin, one JSON answer on stdout.
 */
public final class RasTool {
    private RasTool() {}

    static final String A = "_ras_agent.Agent";

    static final class Failed extends Exception {
        Failed(String m) {
            super(m);
        }
    }

    public static void main(String[] argv) throws Exception {
        Map<String, Object> req = Agent.object(Json.parse(new String(System.in.readAllBytes(), StandardCharsets.UTF_8)));
        Object out;
        try {
            out = switch ((String) req.get("do")) {
                case "inspect" -> new Unit(req).inspect();
                case "build" -> new Unit(req).build();
                case "hardened" -> Hardened.take(req);
                case "place" -> place(req);
                default -> throw new Failed("unknown request");
            };
        } catch (Failed e) {
            out = Json.map("error", e.getMessage());
        }
        System.out.write(Json.write(out).getBytes(StandardCharsets.UTF_8));
        System.out.flush();
    }

    static String str(Map<String, Object> m, String k) {
        return (String) m.get(k);
    }

    static List<String> strs(Object o) {
        List<String> out = new ArrayList<>();
        for (Object x : Agent.list(o)) out.add((String) x);
        return out;
    }

    /** Text edits of one file; insertions at one place keep the order given by `rank`. */
    static final class Edits {
        record E(long from, long to, long rank, String text) {}

        final List<E> list = new ArrayList<>();

        void insert(long at, long rank, String text) {
            list.add(new E(at, at, rank, text));
        }

        void replace(long from, long to, String text) {
            list.add(new E(from, to, 2_000_000, text));
        }

        String apply(String s) {
            list.sort(Comparator.comparingLong(E::from).thenComparingLong(E::rank));
            StringBuilder b = new StringBuilder();
            int pos = 0;
            for (E e : list) {
                if (e.to > e.from && e.from < pos) continue; // one type of several variables: seen once per variable
                b.append(s, pos, (int) Math.max(e.from, pos)).append(e.text);
                pos = (int) Math.max(pos, e.to);
            }
            return b.append(s.substring(pos)).toString();
        }
    }

    // ---------------------------------------------------------------- a program: entry files and all they reach

    static final class Unit {
        final Map<String, Object> req;
        final JavaCompiler javac = ToolProvider.getSystemJavaCompiler();
        final DiagnosticCollector<JavaFileObject> diags = new DiagnosticCollector<>();
        final List<CompilationUnitTree> units = new ArrayList<>();
        final Path src;
        final String origin;
        Trees trees;
        SourcePositions pos;
        JavacTask task;
        final List<Map<String, Object>> functions = new ArrayList<>();
        final List<Object> ranges = new ArrayList<>();
        final Map<String, Integer> byId = new HashMap<>();

        Unit(Map<String, Object> req) throws Failed {
            this.req = req;
            if (javac == null) throw new Failed("this Java has no compiler (a JDK is needed, not a JRE)");
            src = Path.of(str(req, "src"));
            origin = str(req, "origin");
        }

        List<String> options(String... more) {
            List<String> o = new ArrayList<>(List.of("-proc:none", "-nowarn", "-Xlint:none", "-g", "-parameters", "-encoding", "UTF-8", "-implicit:class", "-Xprefer:source",
                    "-Xmaxerrs", "20", "-sourcepath", src.toString(), "-classpath", str(req, "classpath")));
            o.addAll(strs(req.get("javac_opts")));
            o.addAll(List.of(more));
            return o;
        }

        JavacTask task(String... more) {
            StandardJavaFileManager fm = javac.getStandardFileManager(diags, null, StandardCharsets.UTF_8);
            List<Path> entry = new ArrayList<>();
            for (String f : strs(req.get("entry"))) entry.add(src.resolve(f));
            return (JavacTask) javac.getTask(new StringWriter(), fm, diags, options(more), null, fm.getJavaFileObjectsFromPaths(entry));
        }

        String errors() {
            StringBuilder b = new StringBuilder();
            int n = 0;
            for (Diagnostic<? extends JavaFileObject> d : diags.getDiagnostics()) {
                if (d.getKind() != Diagnostic.Kind.ERROR || n++ >= 12) continue;
                String f = d.getSource() == null ? "" : src.relativize(Path.of(d.getSource().getName())).toString();
                b.append(f).append(':').append(d.getLineNumber()).append(": ").append(d.getMessage(null)).append('\n');
            }
            return b.toString();
        }

        void analyze() throws Failed {
            task = task();
            trees = Trees.instance(task);
            pos = trees.getSourcePositions();
            Set<CompilationUnitTree> seen = new LinkedHashSet<>();
            task.addTaskListener(new TaskListener() {
                @Override
                public void finished(TaskEvent e) {
                    if (e.getKind() == TaskEvent.Kind.ANALYZE && e.getCompilationUnit() != null) seen.add(e.getCompilationUnit());
                }
            });
            try {
                task.analyze();
            } catch (IOException e) {
                throw new Failed(e.getMessage());
            }
            if (!errors().isEmpty()) throw new Failed(errors());
            for (CompilationUnitTree u : seen) if (Path.of(u.getSourceFile().getName()).startsWith(src)) units.add(u);
            units.sort(Comparator.comparing(u -> u.getSourceFile().getName()));
        }

        Path rel(CompilationUnitTree u) {
            return src.relativize(Path.of(u.getSourceFile().getName()));
        }

        static String text(CompilationUnitTree u) {
            try {
                return u.getSourceFile().getCharContent(true).toString();
            } catch (IOException e) {
                throw new java.io.UncheckedIOException(e);
            }
        }

        static String pkg(CompilationUnitTree u) {
            return u.getPackageName() == null ? "" : u.getPackageName().toString();
        }

        boolean vendored(CompilationUnitTree u) {
            return pkg(u).equals("_ras_hardened") || pkg(u).endsWith("._ras_hardened");
        }

        Object inspect() throws Failed {
            analyze();
            for (CompilationUnitTree u : units) if (!vendored(u)) new Functions(this, u).scan(u, null);
            return Json.map("functions", functions, "ranges", ranges);
        }

        Object build() throws Failed, IOException {
            analyze();
            for (CompilationUnitTree u : units) if (!vendored(u)) new Functions(this, u).scan(u, null);
            Map<Path, String> out = new LinkedHashMap<>();
            for (CompilationUnitTree u : units) {
                Edits e = new Edits();
                new Rewriter(this, u, e).scan(u, null);
                out.put(Path.of(u.getSourceFile().getName()), e.apply(text(u)));
            }
            for (Map.Entry<Path, String> e : out.entrySet()) Files.writeString(e.getKey(), e.getValue());
            diags.getDiagnostics();
            DiagnosticCollector<JavaFileObject> again = new DiagnosticCollector<>();
            StandardJavaFileManager fm = javac.getStandardFileManager(again, null, StandardCharsets.UTF_8);
            Path classes = Path.of(str(req, "classes"));
            Files.createDirectories(classes);
            boolean ok = javac.getTask(new StringWriter(), fm, again, options("-d", classes.toString()), null,
                    fm.getJavaFileObjectsFromPaths(out.keySet())).call();
            if (!ok) {
                StringBuilder b = new StringBuilder("the instrumented copy doesn't compile:\n");
                for (Diagnostic<? extends JavaFileObject> d : again.getDiagnostics())
                    if (d.getKind() == Diagnostic.Kind.ERROR)
                        b.append(d.getSource() == null ? "" : src.relativize(Path.of(d.getSource().getName()))).append(':').append(d.getLineNumber())
                                .append(": ").append(d.getMessage(null)).append('\n');
                throw new Failed(b.toString());
            }
            return Json.map("functions", functions, "ranges", ranges);
        }
    }

    static String binary(TypeElement t) {
        String n = t.getSimpleName().toString();
        for (Element e = t.getEnclosingElement(); e instanceof TypeElement o; e = e.getEnclosingElement()) n = o.getSimpleName() + "$" + n;
        Element p = t;
        while (p.getKind() != ElementKind.PACKAGE) p = p.getEnclosingElement();
        String pk = p.toString();
        return ((javax.lang.model.element.PackageElement) p).isUnnamed() ? n : pk + "." + n;
    }

    static boolean member(TypeElement t) {
        Element e = t;
        while (e instanceof TypeElement x) {
            if (x.getSimpleName().length() == 0 || (x.getNestingKind() != javax.lang.model.element.NestingKind.TOP_LEVEL
                    && x.getNestingKind() != javax.lang.model.element.NestingKind.MEMBER)) return false;
            e = x.getEnclosingElement();
        }
        return e.getKind() == ElementKind.PACKAGE;
    }

    // ---------------------------------------------------------------- functions and their signals

    static final class Functions extends TreePathScanner<Void, Void> {
        final Unit un;
        final CompilationUnitTree cu;
        final String file, code;
        final LineMap lines;

        Functions(Unit un, CompilationUnitTree cu) {
            this.un = un;
            this.cu = cu;
            file = un.rel(cu).toString().replace('\\', '/');
            code = Unit.text(cu);
            lines = cu.getLineMap();
        }

        @Override
        public Void visitMethod(MethodTree m, Void v) {
            Element el = un.trees.getElement(getCurrentPath());
            if (el instanceof ExecutableElement x && m.getBody() != null && x.getEnclosingElement() instanceof TypeElement owner && member(owner)
                    && un.pos.getStartPosition(cu, m) >= 0 && un.pos.getEndPosition(cu, m) > un.pos.getStartPosition(cu, m))
                add(m, x, owner);
            return super.visitMethod(m, v);
        }

        void add(MethodTree m, ExecutableElement x, TypeElement owner) {
            String name = m.getName().toString(), bin = binary(owner), cls = owner.getQualifiedName().toString();
            long start = lines.getLineNumber(un.pos.getStartPosition(cu, m)), end = lines.getLineNumber(un.pos.getEndPosition(cu, m));
            // what enable put in belongs to the function it hardens
            int cut = bin.indexOf("$_Ras_");
            if (cut >= 0 || name.startsWith("_ras_orig_")) {
                String fn = cut >= 0 ? bin.substring(cut + 6).split("\\$")[0] : name.substring(10);
                String id = (cut >= 0 ? bin.substring(0, cut).replace('$', '.') : cls) + "." + fn;
                un.ranges.add(range(start, end, id));
                return;
            }
            int same = 0;
            for (Element e : owner.getEnclosedElements()) if (e.getKind() == ElementKind.METHOD && e.getSimpleName().contentEquals(name)) same++;
            List<Object> params = new ArrayList<>();
            StringBuilder sig = new StringBuilder();
            String bad = null;
            for (int i = 0; i < m.getParameters().size(); i++) {
                VariableTree p = m.getParameters().get(i);
                boolean var = x.isVarArgs() && i == m.getParameters().size() - 1;
                String type = p.getType() == null ? "" : p.getType().toString();
                if (var) type = type.replaceAll("\\[\\]$", "") + "...";
                params.add(Json.map("name", p.getName().toString(), "type", type));
                sig.append(i == 0 ? "" : ",").append(type.replaceAll("<.*>", ""));
                String why = jsonable(x.getParameters().get(i).asType(), true, new HashSet<>());
                if (why != null && bad == null) bad = p.getName() + ": " + why;
            }
            boolean ctor = x.getKind() == ElementKind.CONSTRUCTOR;
            String id = cls + "." + (ctor ? "<init>" : name) + (same > 1 ? "(" + sig + ")" : "");
            String retBad = ctor ? null : jsonable(x.getReturnType(), false, new HashSet<>());
            boolean stat = x.getModifiers().contains(Modifier.STATIC);
            boolean main = stat && name.equals("main") && params.size() <= 1 && x.getReturnType().getKind() == TypeKind.VOID;
            String refused = ctor ? "a constructor (move the dependency call into a static method)"
                    : main ? null
                    : !stat ? "an instance method (only static methods can be swapped so far)"
                    : same > 1 ? "overloaded (give it a name of its own)"
                    : !x.getTypeParameters().isEmpty() ? "a generic method" : null;
            Map<String, String> signals = new Signals(un, cu, m).found();
            List<String> thrown = new ArrayList<>();
            m.getThrows().forEach(t -> thrown.add(t.toString()));
            long s0 = un.pos.getStartPosition(cu, m);
            int bol = code.lastIndexOf('\n', (int) s0 - 1) + 1;
            String lead = code.substring(bol, (int) s0);
            List<Object> classes = new ArrayList<>();
            for (Tree t : cu.getTypeDecls()) if (t instanceof ClassTree) collect((TypeElement) un.trees.getElement(TreePath.getPath(cu, t)), classes);
            Map<String, Object> f = Json.map("id", id, "file", un.origin + "/" + file, "rel", file, "line", start, "end", end,
                    "name", ctor ? "<init>" : name, "qualname", id.substring(cls.length() + 1), "module", cls, "owner", cls, "binary", bin,
                    "classes", classes, "params", params, "returns", ctor ? "" : m.getReturnType().toString(), "throws", thrown,
                    "static", stat, "main", main, "refused", refused, "arg_ok", bad == null, "ret_ok", retBad == null,
                    "arg_bad", bad, "ret_bad", retBad, "signals", signals,
                    "source", (lead.isBlank() ? lead : "") + code.substring((int) s0, (int) un.pos.getEndPosition(cu, m)));
            un.byId.put(id, un.functions.size());
            un.functions.add(f);
            un.ranges.add(range(start, end, id));
        }

        void collect(TypeElement t, List<Object> out) {
            if (t == null || t.getSimpleName().toString().startsWith("_Ras_")) return;
            out.add(binary(t));
            for (Element e : t.getEnclosedElements()) if (e instanceof TypeElement n) collect(n, out);
        }

        Map<String, Object> range(long start, long end, String id) {
            int slash = file.lastIndexOf('/');
            return Json.map("unit", Unit.pkg(cu) + "/" + file.substring(slash + 1), "start", start, "end", end, "id", id);
        }

        private static final Set<String> PLAIN = Set.of("java.lang.String", "java.lang.Integer", "java.lang.Long", "java.lang.Double", "java.lang.Float",
                "java.lang.Short", "java.lang.Byte", "java.lang.Boolean", "java.lang.Character", "java.lang.Object", "java.lang.Number", "java.lang.CharSequence",
                "java.math.BigDecimal", "java.math.BigInteger", "java.time.Instant", "java.time.LocalDate", "java.time.LocalDateTime", "java.time.OffsetDateTime",
                "java.time.ZonedDateTime", "java.time.Duration", "java.util.UUID", "java.net.URI");
        private static final Set<String> GENERIC = Set.of("java.util.List", "java.util.ArrayList", "java.util.Collection", "java.lang.Iterable", "java.util.Set",
                "java.util.LinkedHashSet", "java.util.Optional");
        private static final Set<String> MAPS = Set.of("java.util.Map", "java.util.LinkedHashMap", "java.util.HashMap", "java.util.TreeMap", "java.util.SortedMap");

        /** null, or why values of this type can't be stored. */
        String jsonable(TypeMirror t, boolean arg, Set<String> seen) {
            if (t.getKind().isPrimitive() || (t.getKind() == TypeKind.VOID && !arg)) return null;
            if (t instanceof ArrayType a) return jsonable(a.getComponentType(), arg, seen);
            if (!(t instanceof DeclaredType d) || !(d.asElement() instanceof TypeElement e)) return t + " isn't plain data";
            String n = e.getQualifiedName().toString();
            if (PLAIN.contains(n) || e.getKind() == ElementKind.ENUM || (arg && n.equals("java.net.http.HttpClient"))) return null;
            if (GENERIC.contains(n) || MAPS.contains(n)) {
                if (d.getTypeArguments().isEmpty()) return "a raw " + n;
                if (MAPS.contains(n) && !d.getTypeArguments().get(0).toString().equals("java.lang.String")) return n + " with keys other than String";
                for (TypeMirror a : d.getTypeArguments()) {
                    String why = jsonable(a, arg, seen);
                    if (why != null) return why;
                }
                return null;
            }
            if (e.getKind() == ElementKind.RECORD) {
                if (!e.getTypeParameters().isEmpty()) return n + " is generic";
                if (!seen.add(n)) return null;
                for (RecordComponentElement c : e.getRecordComponents()) {
                    String why = jsonable(c.asType(), arg, seen);
                    if (why != null) return why;
                }
                return null;
            }
            return n + " isn't plain data (a primitive, String, List/Map/Optional, enum or record of those)";
        }
    }

    /** Reliability signals of one method (signals.SIGNALS). */
    static final class Signals {
        final Unit un;
        final CompilationUnitTree cu;
        final MethodTree m;
        final Set<String> calls = new LinkedHashSet<>();
        boolean timeout, loop, loopCall, key, logs, swallow, lost;
        String verb, handler;
        int depth;

        Signals(Unit un, CompilationUnitTree cu, MethodTree m) {
            this.un = un;
            this.cu = cu;
            this.m = m;
        }

        Map<String, String> found() {
            new TreePathScanner<Void, Void>() {
                @Override
                public Void visitMethodInvocation(MethodInvocationTree t, Void v) {
                    Element e = un.trees.getElement(getCurrentPath());
                    String o = e != null && e.getEnclosingElement() instanceof TypeElement te ? te.getQualifiedName().toString() : "", n = e == null ? "" : e.getSimpleName().toString();
                    int argc = t.getArguments().size();
                    String hit = switch (o) {
                        case "java.net.http.HttpClient" -> n.equals("send") || n.equals("sendAsync") ? "HttpClient." + n : null;
                        case "java.net.URL" -> n.equals("openConnection") || n.equals("openStream") ? "URL." + n : null;
                        case "java.lang.ProcessBuilder" -> n.equals("start") ? "ProcessBuilder.start" : null;
                        case "java.lang.Runtime" -> n.equals("exec") ? "Runtime.exec" : null;
                        default -> null;
                    };
                    if (hit != null) {
                        calls.add(hit);
                        loopCall |= depth > 0;
                    }
                    if ((o.equals("java.net.http.HttpRequest.Builder") && n.equals("timeout")) || n.equals("setReadTimeout") || n.equals("orTimeout") || n.equals("completeOnTimeout")
                            || (o.equals("java.lang.Process") && n.equals("waitFor") && argc == 2)
                            || (n.equals("get") && argc == 2 && (o.equals("java.util.concurrent.Future") || o.equals("java.util.concurrent.CompletableFuture")))) timeout = true;
                    if (o.equals("java.net.http.HttpRequest.Builder") && Set.of("POST", "PUT", "DELETE").contains(n)) verb = n;
                    if ((n.equals("method") || n.equals("setRequestMethod")) && argc > 0 && t.getArguments().get(0) instanceof LiteralTree l
                            && Set.of("POST", "PUT", "PATCH", "DELETE").contains(String.valueOf(l.getValue()))) verb = String.valueOf(l.getValue());
                    if (o.equals("java.util.logging.Logger") || o.equals("java.lang.System.Logger") || o.endsWith(".Logger") || t.getMethodSelect().toString().startsWith("System.err")) logs = true;
                    if (n.equals("printStackTrace")) logs = true;
                    return super.visitMethodInvocation(t, v);
                }

                @Override
                public Void visitLiteral(LiteralTree t, Void v) {
                    if (t.getValue() instanceof String s && s.equalsIgnoreCase("Idempotency-Key")) key = true;
                    return null;
                }

                Void looped(Runnable r) {
                    depth++;
                    r.run();
                    depth--;
                    return null;
                }

                @Override
                public Void visitForLoop(ForLoopTree t, Void v) {
                    return looped(() -> super.visitForLoop(t, v));
                }

                @Override
                public Void visitWhileLoop(WhileLoopTree t, Void v) {
                    return looped(() -> super.visitWhileLoop(t, v));
                }

                @Override
                public Void visitDoWhileLoop(DoWhileLoopTree t, Void v) {
                    return looped(() -> super.visitDoWhileLoop(t, v));
                }

                @Override
                public Void visitCatch(CatchTree t, Void v) {
                    boolean[] has = new boolean[2];
                    String name = t.getParameter().getName().toString();
                    new TreeScanner<Void, Void>() {
                        @Override
                        public Void visitThrow(ThrowTree x, Void q) {
                            has[0] = true;
                            return super.visitThrow(x, q);
                        }

                        @Override
                        public Void visitIdentifier(IdentifierTree x, Void q) {
                            has[1] |= x.getName().contentEquals(name);
                            return null;
                        }
                    }.scan(t.getBlock(), null);
                    if (!has[0] && depth == 0) {
                        swallow = true;
                        handler = "catch (" + t.getParameter().getType() + " " + name + ")";
                    }
                    if (has[0] && !has[1]) lost = true;
                    return super.visitCatch(t, v);
                }
            }.scan(TreePath.getPath(cu, m), null);
            Map<String, String> out = new TreeMap<>();
            if (calls.isEmpty()) return out;
            String call = calls.iterator().next();
            if (!timeout) out.put("no_timeout", call + "() without timeout");
            if (!loopCall) out.put("no_retry", "no retry on transient errors");
            if (verb != null && !key) {
                if (loopCall) out.put("unsafe_retry", "retries a " + verb + " with no Idempotency-Key");
                else out.put("write_no_key", verb + " without Idempotency-Key");
            }
            if (swallow) out.put("swallow", "`" + handler + "` swallows errors");
            if (lost) out.put("no_context", "rethrows without the cause");
            if (!logs) out.put("no_logging", "no logging on the failure path");
            return out;
        }
    }

    // ---------------------------------------------------------------- instrumentation

    static final class Rewriter extends TreePathScanner<Void, Void> {
        final Unit un;
        final CompilationUnitTree cu;
        final Edits ed;
        final String code;
        final boolean plain; // the vendored runtime: boundaries and clock only
        final String mode;
        final Set<String> wrapped;
        long seq;

        Rewriter(Unit un, CompilationUnitTree cu, Edits ed) {
            this.un = un;
            this.cu = cu;
            this.ed = ed;
            code = Unit.text(cu);
            plain = un.vendored(cu);
            mode = str(un.req, "mode");
            wrapped = new HashSet<>(strs(un.req.get("targets")));
        }

        long start(Tree t) {
            return un.pos.getStartPosition(cu, t);
        }

        long end(Tree t) {
            return un.pos.getEndPosition(cu, t);
        }

        /** Around an expression: outer ones open first and close last. */
        void around(Tree t, String open, String close) {
            ed.insert(start(t), 1_000_000 + seq, open);
            ed.insert(end(t), -1 - seq, close);
            seq++;
        }

        @Override
        public Void visitMethod(MethodTree m, Void v) {
            Element el = un.trees.getElement(getCurrentPath());
            if (!plain && !mode.equals("verify") && el instanceof ExecutableElement x && x.getKind() == ElementKind.METHOD && m.getBody() != null
                    && x.getEnclosingElement() instanceof TypeElement owner && member(owner) && !binary(owner).contains("$_Ras_")
                    && !m.getName().toString().startsWith("_ras_orig_")) {
                for (Map<String, Object> f : un.functions) {
                    if (!f.get("owner").equals(owner.getQualifiedName().toString()) || !f.get("name").equals(m.getName().toString())
                            || !f.get("line").equals(cu.getLineMap().getLineNumber(start(m)))) continue;
                    int id = un.byId.get(str(f, "id"));
                    ed.insert(start(m.getBody()) + 1, 0, " " + A + ".enter(" + id + ");");
                    if (wrapped.contains(str(f, "id")) && f.get("refused") == null) wrap(m, f, id);
                }
            }
            return super.visitMethod(m, v);
        }

        void wrap(MethodTree m, Map<String, Object> f, int id) {
            String name = m.getName().toString();
            int at = nameAt(code, cu, un.pos, m);
            ed.replace(at, at + name.length(), "_ras_body_" + name);
            List<String> ps = new ArrayList<>(), as = new ArrayList<>();
            for (Object o : Agent.list(f.get("params"))) {
                Map<String, Object> p = Agent.object(o);
                ps.add(p.get("type") + " " + p.get("name"));
                as.add(str(p, "name"));
            }
            List<String> thr = strs(f.get("throws"));
            boolean isVoid = f.get("returns").equals("void");
            String mods = "";
            for (Modifier x : m.getModifiers().getFlags()) if (x != Modifier.SYNCHRONIZED) mods += x + " ";
            String call = "(" + id + ", new Object[] {" + String.join(", ", as) + "}, () -> _ras_body_" + name + "(" + String.join(", ", as) + "));";
            ed.insert(start(m), 0, mods + f.get("returns") + " " + name + "(" + String.join(", ", ps) + ")" + (thr.isEmpty() ? "" : " throws " + String.join(", ", thr))
                    + " { " + (isVoid ? A + ".<RuntimeException>callVoid" : "return " + A + ".<" + boxed(str(f, "returns")) + ", RuntimeException>call") + call + " } ");
        }

        static String boxed(String t) {
            return switch (t) {
                case "int" -> "Integer";
                case "long" -> "Long";
                case "double" -> "Double";
                case "float" -> "Float";
                case "short" -> "Short";
                case "byte" -> "Byte";
                case "boolean" -> "Boolean";
                case "char" -> "Character";
                default -> t;
            };
        }

        private static final Set<String> STATICS = Set.of("java.lang.Thread#sleep", "java.lang.System#currentTimeMillis", "java.lang.System#nanoTime",
                "java.time.Instant#now", "java.lang.Math#random", "java.lang.System#exit");
        private static final Set<String> RECEIVERS = Set.of("java.net.URL#openConnection", "java.net.URL#openStream", "java.lang.ProcessBuilder#start",
                "java.lang.Runtime#exec", "java.util.concurrent.CompletableFuture#join", "java.util.concurrent.CompletableFuture#orTimeout",
                "java.util.concurrent.CompletableFuture#completeOnTimeout", "java.util.concurrent.CompletableFuture#get", "java.util.concurrent.Future#get",
                "java.util.concurrent.TimeUnit#sleep");

        @Override
        public Void visitMethodInvocation(MethodInvocationTree t, Void v) {
            Element e = un.trees.getElement(getCurrentPath());
            if (e instanceof ExecutableElement x && e.getEnclosingElement() instanceof TypeElement o && start(t) >= 0) {
                String n = x.getSimpleName().toString(), k = o.getQualifiedName() + "#" + n;
                if (STATICS.contains(k) && !(n.equals("now") && !t.getArguments().isEmpty()))
                    ed.replace(start(t.getMethodSelect()), end(t.getMethodSelect()), A + "." + n);
                else if (k.equals("java.net.http.HttpClient#newHttpClient") || k.equals("java.net.http.HttpClient.Builder#build")) around(t, A + ".wrap(", ")");
                else if (RECEIVERS.contains(k) && t.getMethodSelect() instanceof MemberSelectTree s) {
                    ed.insert(start(s.getExpression()), 1_000_000 + seq++, A + "." + n + "(");
                    // ".name(" or ".name( " up to the first argument or the closing parenthesis
                    int open = code.indexOf('(', (int) end(s));
                    ed.replace(end(s.getExpression()), open + 1, t.getArguments().isEmpty() ? "" : ", ");
                }
            }
            return super.visitMethodInvocation(t, v);
        }

        @Override
        public Void visitMemberReference(MemberReferenceTree t, Void v) {
            Element e = un.trees.getElement(getCurrentPath());
            if (e instanceof ExecutableElement && e.getEnclosingElement() instanceof TypeElement o) {
                String k = o.getQualifiedName() + "#" + e.getSimpleName();
                if (STATICS.contains(k)) ed.replace(start(t.getQualifierExpression()), end(t.getQualifierExpression()), A);
                if (k.equals("java.util.concurrent.CompletableFuture#join")) ed.replace(start(t.getQualifierExpression()), end(t.getQualifierExpression()), A);
            }
            return super.visitMemberReference(t, v);
        }

        @Override
        public Void visitNewClass(NewClassTree t, Void v) {
            Element e = un.trees.getElement(getCurrentPath());
            if (e != null && e.getEnclosingElement() instanceof TypeElement o && o.getQualifiedName().contentEquals("java.util.Random")
                    && t.getArguments().isEmpty() && t.getClassBody() == null)
                ed.replace(start(t), end(t), A + ".newRandom()");
            return super.visitNewClass(t, v);
        }

        @Override
        public Void visitMemberSelect(MemberSelectTree t, Void v) {
            // ThreadLocalRandom.current().nextX(...): a seeded one
            if (t.getExpression() instanceof MethodInvocationTree c && c.getMethodSelect().toString().endsWith("ThreadLocalRandom.current")) {
                ed.replace(start(c), end(c), A + ".rnd()");
                return null;
            }
            return super.visitMemberSelect(t, v);
        }
    }

    static int nameAt(String code, CompilationUnitTree cu, SourcePositions pos, MethodTree m) {
        long from = m.getReturnType() != null ? pos.getEndPosition(cu, m.getReturnType()) : pos.getStartPosition(cu, m);
        java.util.regex.Matcher x = java.util.regex.Pattern.compile("(?<![\\w$])" + java.util.regex.Pattern.quote(m.getName().toString()) + "(?=\\s*\\()").matcher(code);
        return x.find((int) from) ? x.start() : (int) from;
    }

    // ---------------------------------------------------------------- enable: where things are in a module

    /** Parses one file only. {"file", "owner", "name"} -> the end of the owner class, and the function's declaration. */
    static Object place(Map<String, Object> req) throws Exception {
        String code = str(req, "text"), owner = str(req, "owner"), name = str(req, "name");
        Parsed p = new Parsed(code, "M.java");
        Map<String, Object> out = Json.map("class_end", null, "name_at", null, "imports", p.imports());
        p.classes((path, c) -> {
            if (!path.equals(owner)) return;
            int end = (int) p.pos.getEndPosition(p.cu, c);
            out.put("class_end", end - 1);
            for (Tree t : c.getMembers())
                if (t instanceof MethodTree m && m.getName().contentEquals(name)) {
                    out.put("name_at", nameAt(code, p.cu, p.pos, m));
                    out.put("decl_at", (int) p.pos.getStartPosition(p.cu, m));
                    StringBuilder mods = new StringBuilder();
                    for (Modifier x : m.getModifiers().getFlags()) if (x != Modifier.SYNCHRONIZED) mods.append(x).append(' ');
                    out.put("modifiers", mods.toString());
                }
        });
        return out;
    }

    /** One parsed (not attributed) source text. */
    static final class Parsed {
        final String code;
        final CompilationUnitTree cu;
        final SourcePositions pos;

        Parsed(String code, String fileName) throws Failed {
            this.code = code;
            JavaCompiler javac = ToolProvider.getSystemJavaCompiler();
            if (javac == null) throw new Failed("this Java has no compiler (a JDK is needed, not a JRE)");
            DiagnosticCollector<JavaFileObject> diags = new DiagnosticCollector<>();
            JavaFileObject f = new javax.tools.SimpleJavaFileObject(java.net.URI.create("mem:///" + fileName), JavaFileObject.Kind.SOURCE) {
                @Override
                public CharSequence getCharContent(boolean ignore) {
                    return code;
                }
            };
            JavacTask t = (JavacTask) javac.getTask(new StringWriter(), null, diags, List.of("-proc:none"), null, List.of(f));
            try {
                cu = t.parse().iterator().next();
            } catch (IOException e) {
                throw new Failed(e.getMessage());
            }
            for (Diagnostic<? extends JavaFileObject> d : diags.getDiagnostics())
                if (d.getKind() == Diagnostic.Kind.ERROR) throw new Failed(fileName + ":" + d.getLineNumber() + ": " + d.getMessage(null));
            pos = Trees.instance(t).getSourcePositions();
        }

        List<Object> imports() {
            List<Object> out = new ArrayList<>();
            for (ImportTree i : cu.getImports()) out.add((i.isStatic() ? "static " : "") + i.getQualifiedIdentifier());
            return out;
        }

        void classes(java.util.function.BiConsumer<String, ClassTree> each) {
            String pk = cu.getPackageName() == null ? "" : cu.getPackageName() + ".";
            for (Tree t : cu.getTypeDecls()) if (t instanceof ClassTree c) walk(pk + c.getSimpleName(), c, each);
        }

        private void walk(String path, ClassTree c, java.util.function.BiConsumer<String, ClassTree> each) {
            each.accept(path, c);
            for (Tree t : c.getMembers()) if (t instanceof ClassTree n) walk(path + "." + n.getSimpleName(), n, each);
        }
    }

    // ---------------------------------------------------------------- hardened files

    static final class Hardened {
        private Hardened() {}

        private static final List<String> FORBIDDEN = List.of("_ras_agent", "java.lang.reflect", "java.lang.invoke", "java.lang.instrument", "sun.", "jdk.internal",
                "java.net.Socket", "java.nio.channels.SocketChannel", "com.sun.tools", "com.sun.source", "javax.tools");

        /** The body of class Hardened, ready to stand inside another class: names that came from imports the module doesn't have are spelled out. */
        static Object take(Map<String, Object> req) throws Failed {
            String code = str(req, "text"), name = str(req, "name"), rename = str(req, "rename");
            Set<String> have = new HashSet<>(strs(req.get("imports")));
            Parsed p = new Parsed(code, "hardened.java");
            List<ClassTree> types = new ArrayList<>();
            for (Tree t : p.cu.getTypeDecls()) if (t instanceof ClassTree c) types.add(c);
            if (types.size() != 1 || !types.get(0).getSimpleName().contentEquals("Hardened"))
                throw new Failed("hardened.java must hold one class, `final class Hardened` (put helper types inside it)");
            ClassTree h = types.get(0);
            boolean[] fn = {false, false};
            for (Tree t : h.getMembers()) {
                if (t instanceof MethodTree m && m.getName().contentEquals(name) && m.getModifiers().getFlags().contains(Modifier.STATIC)) fn[0] = true;
                if (t instanceof MethodTree m && m.getName().contentEquals("RAS_FALLBACK")) fn[1] = true;
                if (t instanceof VariableTree x && x.getName().contentEquals("RAS_FALLBACK")) fn[1] = true;
            }
            if (!fn[0]) throw new Failed("class Hardened must keep `static ... " + name + "(...)`");

            Map<String, String> single = new HashMap<>(), statics = new HashMap<>();
            List<String> stars = new ArrayList<>();
            for (ImportTree i : p.cu.getImports()) {
                String q = i.getQualifiedIdentifier().toString(), simple = q.substring(q.lastIndexOf('.') + 1), pk = q.substring(0, q.lastIndexOf('.'));
                if (have.contains((i.isStatic() ? "static " : "") + q)) continue;
                if (i.isStatic()) {
                    if (simple.equals("*")) throw new Failed("spell out `import static " + q + "`");
                    statics.put(simple, q);
                } else if (simple.equals("*")) stars.add(pk);
                else single.put(simple, q);
            }
            Edits ed = new Edits();
            Set<String> bad = new LinkedHashSet<>();
            for (ImportTree i : p.cu.getImports()) check(i.getQualifiedIdentifier().toString(), bad);
            new TreeScanner<Void, Void>() {
                @Override
                public Void visitIdentifier(IdentifierTree t, Void v) {
                    String n = t.getName().toString(), q = null;
                    if (n.equals("Hardened")) q = rename;
                    else if (single.containsKey(n)) q = single.get(n);
                    else if (statics.containsKey(n)) q = statics.get(n);
                    else if (!n.isEmpty() && Character.isUpperCase(n.charAt(0)) && !declared.contains(n))
                        for (String s : stars) {
                            try {
                                Class.forName(s + "." + n, false, ClassLoader.getPlatformClassLoader());
                                q = s + "." + n;
                            } catch (ClassNotFoundException | LinkageError e) {
                                // not from this package
                            }
                        }
                    long at = p.pos.getStartPosition(p.cu, t);
                    if (q != null && at >= 0) ed.replace(at, at + n.length(), q);
                    if (n.equals("Unsafe")) bad.add("Unsafe");
                    return null;
                }

                final Set<String> declared = new HashSet<>();

                {
                    p.classes((path, c) -> declared.add(c.getSimpleName().toString()));
                }

                @Override
                public Void visitMemberSelect(MemberSelectTree t, Void v) {
                    check(t.toString(), bad);
                    if (t.getIdentifier().contentEquals("setAccessible") || t.getIdentifier().contentEquals("getDeclaredMethod") || t.getIdentifier().contentEquals("getDeclaredField")) bad.add("reflection");
                    return super.visitMemberSelect(t, v);
                }

                @Override
                public Void visitMethod(MethodTree m, Void v) {
                    if (m.getReturnType() == null && m.getName().contentEquals("<init>") && p.pos.getEndPosition(p.cu, m) > 0) {
                        int at = p.code.indexOf("Hardened", (int) p.pos.getStartPosition(p.cu, m));
                        if (at >= 0 && at < p.pos.getStartPosition(p.cu, m.getBody())) ed.replace(at, at + 8, rename);
                    }
                    return super.visitMethod(m, v);
                }
            }.scan(h, null);
            long from = p.pos.getStartPosition(p.cu, h), end = p.pos.getEndPosition(p.cu, h);
            int open = bodyOpen(p, h, (int) from);
            // only edits inside the body
            ed.list.removeIf(e -> e.from() <= open || e.to() >= end);
            List<Edits.E> shifted = new ArrayList<>();
            for (Edits.E e : ed.list) shifted.add(new Edits.E(e.from() - open - 1, e.to() - open - 1, e.rank(), e.text()));
            ed.list.clear();
            ed.list.addAll(shifted);
            String body = ed.apply(code.substring(open + 1, (int) end - 1));
            return Json.map("body", body, "forbidden", new ArrayList<>(bad), "has_fallback", fn[1]);
        }

        private static int bodyOpen(Parsed p, ClassTree h, int from) {
            int at = p.code.indexOf("Hardened", from + 0);
            // past modifiers and annotations: the class's own name comes after the keyword
            java.util.regex.Matcher m = java.util.regex.Pattern.compile("\\bclass\\s+Hardened\\b").matcher(p.code);
            if (m.find(from)) at = m.end();
            return p.code.indexOf('{', at);
        }

        private static void check(String q, Set<String> bad) {
            for (String f : FORBIDDEN) if (q.equals(f) || q.startsWith(f.endsWith(".") ? f : f + ".") || (f.equals("_ras_agent") && q.contains("_ras_agent"))) bad.add(f);
        }
    }
}
