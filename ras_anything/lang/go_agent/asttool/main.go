// Command asttool inspects and instruments Go projects for ras-anything.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// --------------------------------------------------------------------------- data types

type ParamInfo struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

type FnInfo struct {
	ID           string      `json:"id"`
	Qualname     string      `json:"qualname"`
	Name         string      `json:"name"`
	Module       string      `json:"module"`
	PkgName      string      `json:"pkg_name"`
	PkgDir       string      `json:"pkg_dir"`
	File         string      `json:"file"`
	Line         int         `json:"line"`
	EndLine      int         `json:"end_line"`
	Exported     bool        `json:"exported"`
	Receiver     string      `json:"receiver"`
	RecvVar      string      `json:"recv_var"`
	Params       []ParamInfo `json:"params"`
	Results      []ParamInfo `json:"results"`
	ArgOK        bool        `json:"arg_ok"`
	RetOK        bool        `json:"ret_ok"`
	IsMain       bool        `json:"is_main"`
	Source       string      `json:"source"`
	Signals      []string    `json:"signals"`
	BoundaryCnt  int         `json:"boundary_cnt"`
	HardenReason string      `json:"harden_reason"`
}

type InspectOut struct {
	Module    string   `json:"module"`
	GoVersion string   `json:"go_version"`
	Functions []FnInfo `json:"functions"`
}

type InstrumentSpec struct {
	WorkDir       string   `json:"work_dir"`
	Mode          string   `json:"mode"` // scan | chaos | record | bench | verify
	Module        string   `json:"module"`
	AgentImport   string   `json:"agent_import"`
	RuntimeImport string   `json:"runtime_import"`
	TargetFID     string   `json:"target_fid"`
	BenchFIDs     []string `json:"bench_fids"`
	HardenedFile  string   `json:"hardened_file"`
}

// --------------------------------------------------------------------------- helpers

func readGoMod(root string) (string, string) {
	b, err := os.ReadFile(filepath.Join(root, "go.mod"))
	if err != nil {
		return filepath.Base(root), "1.22"
	}
	mod, ver := filepath.Base(root), "1.22"
	for _, ln := range strings.Split(string(b), "\n") {
		ln = strings.TrimSpace(ln)
		if strings.HasPrefix(ln, "module ") {
			mod = strings.TrimSpace(strings.TrimPrefix(ln, "module "))
		} else if strings.HasPrefix(ln, "go ") {
			ver = strings.TrimSpace(strings.TrimPrefix(ln, "go "))
		}
	}
	return mod, ver
}

func exprString(fset *token.FileSet, e any) string {
	var buf bytes.Buffer
	_ = printer.Fprint(&buf, fset, e)
	return buf.String()
}

func isJSONSerializableType(ts string, allowCtx bool) bool {
	ts = strings.TrimSpace(ts)
	if allowCtx && ts == "context.Context" {
		return true
	}
	if strings.HasPrefix(ts, "chan ") || strings.HasPrefix(ts, "<-chan") || strings.HasPrefix(ts, "chan<-") ||
		strings.HasPrefix(ts, "func(") || ts == "io.Reader" || ts == "io.Writer" || ts == "io.ReadCloser" ||
		ts == "*http.Request" || ts == "http.ResponseWriter" || ts == "*http.Response" || ts == "*http.Client" {
		return false
	}
	return true
}

func walkGoFiles(root string) ([]string, error) {
	var files []string
	err := filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		name := d.Name()
		if d.IsDir() {
			if name == ".git" || name == ".ras" || name == "vendor" || name == "node_modules" || strings.HasPrefix(name, "_ras_") {
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(name, ".go") && !strings.HasSuffix(name, "_test.go") && !strings.HasPrefix(name, "ras_") {
			files = append(files, p)
		}
		return nil
	})
	sort.Strings(files)
	return files, err
}

func pkgModulePrefix(pkgRel, pkgName string) string {
	if pkgRel == "." || pkgRel == "" {
		return pkgName
	}
	return strings.ReplaceAll(filepath.ToSlash(pkgRel), "/", ".")
}

func baseRecvType(recv string) string {
	s := strings.TrimSpace(strings.TrimPrefix(recv, "*"))
	if idx := strings.IndexByte(s, '['); idx > 0 {
		s = s[:idx]
	}
	return s
}

func analyzeSignals(fn *ast.FuncDecl, fset *token.FileSet) ([]string, int) {
	if fn.Body == nil {
		return nil, 0
	}
	var (
		hasHTTP       bool
		hasNonIdem    bool
		hasTimeout    bool
		hasRetry      bool
		hasIdemKey    bool
		hasSubproc    bool
		hasSubTimeout bool
		hasBreaker    bool
		swallowErr    bool
		boundaryCnt   int
	)
	ast.Inspect(fn.Body, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.ForStmt, *ast.RangeStmt:
			// Simple loop inside boundary-calling func suggests retry or iteration; refine below
		case *ast.CompositeLit:
			ts := exprString(fset, x.Type)
			if ts == "http.Client" {
				for _, elt := range x.Elts {
					if kv, ok := elt.(*ast.KeyValueExpr); ok {
						if id, ok := kv.Key.(*ast.Ident); ok && id.Name == "Timeout" {
							hasTimeout = true
						}
					}
				}
			}
		case *ast.CallExpr:
			callee := exprString(fset, x.Fun)
			switch {
			case callee == "http.Get" || callee == "http.Head":
				hasHTTP = true
				boundaryCnt++
			case callee == "http.Post" || callee == "http.PostForm":
				hasHTTP = true
				hasNonIdem = true
				boundaryCnt++
			case callee == "http.NewRequest":
				hasHTTP = true
				boundaryCnt++
				if len(x.Args) > 0 {
					m := strings.Trim(exprString(fset, x.Args[0]), `"`)
					if m == "POST" || m == "PUT" || m == "PATCH" || m == "DELETE" {
						hasNonIdem = true
					}
				}
			case callee == "http.NewRequestWithContext":
				hasHTTP = true
				hasTimeout = true
				boundaryCnt++
				if len(x.Args) > 1 {
					m := strings.Trim(exprString(fset, x.Args[1]), `"`)
					if m == "POST" || m == "PUT" || m == "PATCH" || m == "DELETE" {
						hasNonIdem = true
					}
				}
			case strings.HasSuffix(callee, ".Do"):
				hasHTTP = true
				boundaryCnt++
			case callee == "exec.Command":
				hasSubproc = true
				boundaryCnt++
			case callee == "exec.CommandContext":
				hasSubproc = true
				hasSubTimeout = true
				boundaryCnt++
			case callee == "context.WithTimeout" || callee == "context.WithDeadline":
				hasTimeout = true
				hasSubTimeout = true
			case strings.Contains(callee, "Retry") || strings.Contains(callee, "retry"):
				hasRetry = true
			case strings.Contains(callee, "Allow") || strings.Contains(callee, "Breaker"):
				hasBreaker = true
			case strings.HasSuffix(callee, ".Set") || strings.HasSuffix(callee, ".Add"):
				if len(x.Args) > 0 && strings.Contains(strings.ToLower(exprString(fset, x.Args[0])), "idempotency-key") {
					hasIdemKey = true
				}
			}
		case *ast.AssignStmt:
			// Check `_ = ...` swallowing error on boundary/decode
			if len(x.Lhs) >= 1 {
				lastLhs := exprString(fset, x.Lhs[len(x.Lhs)-1])
				if lastLhs == "_" && len(x.Rhs) == 1 {
					rhs := exprString(fset, x.Rhs[0])
					if strings.Contains(rhs, "Do(") || strings.Contains(rhs, "Get(") ||
						strings.Contains(rhs, "Post(") || strings.Contains(rhs, "Output(") || strings.Contains(rhs, "Run(") {
						swallowErr = true
					}
				}
			}
		case *ast.IfStmt:
			cond := exprString(fset, x.Cond)
			if strings.Contains(cond, "err != nil") {
				// check if body returns nil as last error return
				if x.Body != nil {
					for _, st := range x.Body.List {
						if ret, ok := st.(*ast.ReturnStmt); ok && len(ret.Results) > 0 {
							last := exprString(fset, ret.Results[len(ret.Results)-1])
							if last == "nil" {
								swallowErr = true
							}
						}
					}
				}
			}
		}
		return true
	})

	// Check if there is a retry loop (for loop containing sleep/timer or attempt)
	if !hasRetry && boundaryCnt > 0 {
		ast.Inspect(fn.Body, func(n ast.Node) bool {
			if fs, ok := n.(*ast.ForStmt); ok {
				bodyStr := exprString(fset, fs)
				if strings.Contains(bodyStr, "Sleep") || strings.Contains(bodyStr, "NewTimer") ||
					strings.Contains(bodyStr, "backoff") || strings.Contains(bodyStr, "attempt") || strings.Contains(bodyStr, "retry") {
					hasRetry = true
				}
			}
			return true
		})
	}

	var sigs []string
	if (hasHTTP && !hasTimeout) || (hasSubproc && !hasSubTimeout) {
		sigs = append(sigs, "no_timeout")
	}
	if hasNonIdem && !hasIdemKey {
		sigs = append(sigs, "write_no_key")
	}
	if (hasHTTP || hasSubproc) && !hasRetry {
		sigs = append(sigs, "no_retry")
	}
	if swallowErr {
		sigs = append(sigs, "swallow")
	}
	_ = hasBreaker
	return sigs, boundaryCnt
}

// --------------------------------------------------------------------------- inspect

func cmdInspect(root string) error {
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return err
	}
	mod, goVer := readGoMod(absRoot)
	files, err := walkGoFiles(absRoot)
	if err != nil {
		return err
	}
	fset := token.NewFileSet()
	var out []FnInfo

	for _, fpath := range files {
		src, err := os.ReadFile(fpath)
		if err != nil {
			return err
		}
		af, err := parser.ParseFile(fset, fpath, src, parser.ParseComments)
		if err != nil {
			continue
		}
		rel, _ := filepath.Rel(absRoot, fpath)
		rel = filepath.ToSlash(rel)
		pkgRel := filepath.ToSlash(filepath.Dir(rel))
		pkgName := af.Name.Name
		modPref := pkgModulePrefix(pkgRel, pkgName)

		for _, decl := range af.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			name := fn.Name.Name
			if name == "init" || strings.HasPrefix(name, "_ras_") {
				continue
			}
			var recvType, recvVar string
			qualname := name
			if fn.Recv != nil && len(fn.Recv.List) > 0 {
				recvType = exprString(fset, fn.Recv.List[0].Type)
				if len(fn.Recv.List[0].Names) > 0 {
					recvVar = fn.Recv.List[0].Names[0].Name
				}
				qualname = baseRecvType(recvType) + "." + name
			}
			fid := modPref + "." + qualname
			isMain := pkgName == "main" && name == "main" && fn.Recv == nil

			var params []ParamInfo
			argOK := !isMain && fn.Recv == nil // methods on stateful receivers can't be trivially JSON-constructed in isolated replay unless static
			if fn.Type.Params != nil {
				for idx, field := range fn.Type.Params.List {
					ts := exprString(fset, field.Type)
					allowCtx := idx == 0
					if !isJSONSerializableType(ts, allowCtx) {
						argOK = false
					}
					if len(field.Names) == 0 {
						params = append(params, ParamInfo{Name: fmt.Sprintf("arg%d", len(params)), Type: ts})
					} else {
						for _, nm := range field.Names {
							params = append(params, ParamInfo{Name: nm.Name, Type: ts})
						}
					}
				}
			}
			var results []ParamInfo
			retOK := !isMain
			if fn.Type.Results != nil {
				nRes := len(fn.Type.Results.List)
				for idx, field := range fn.Type.Results.List {
					ts := exprString(fset, field.Type)
					if ts == "error" {
						if idx != nRes-1 {
							retOK = false
						}
					} else if !isJSONSerializableType(ts, false) {
						retOK = false
					}
					if len(field.Names) == 0 {
						results = append(results, ParamInfo{Name: "", Type: ts})
					} else {
						for _, nm := range field.Names {
							results = append(results, ParamInfo{Name: nm.Name, Type: ts})
						}
					}
				}
			}
			if params == nil {
				params = []ParamInfo{}
			}
			if results == nil {
				results = []ParamInfo{}
			}
			startPos := fset.Position(fn.Pos())
			endPos := fset.Position(fn.End())
			startOff := startPos.Offset
			endOff := endPos.Offset
			if startOff < 0 || endOff > len(src) || startOff >= endOff {
				continue
			}
			sigs, bcnt := analyzeSignals(fn, fset)
			if sigs == nil {
				sigs = []string{}
			}
			reason := ""
			if len(sigs) > 0 {
				reason = strings.Join(sigs, ", ")
			}
			out = append(out, FnInfo{
				ID: fid, Qualname: qualname, Name: name, Module: modPref,
				PkgName: pkgName, PkgDir: pkgRel, File: rel,
				Line: startPos.Line, EndLine: endPos.Line,
				Exported: ast.IsExported(name), Receiver: recvType, RecvVar: recvVar,
				Params: params, Results: results, ArgOK: argOK, RetOK: retOK, IsMain: isMain,
				Source: string(src[startOff:endOff]), Signals: sigs, BoundaryCnt: bcnt, HardenReason: reason,
			})
		}
	}

	if out == nil {
		out = []FnInfo{}
	}
	res := InspectOut{Module: mod, GoVersion: goVer, Functions: out}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(res)
}

// --------------------------------------------------------------------------- source edit engine

type edit struct {
	start int
	end   int
	rep   string
}

func applyEdits(src []byte, edits []edit) []byte {
	sort.Slice(edits, func(i, j int) bool {
		if edits[i].start != edits[j].start {
			return edits[i].start > edits[j].start
		}
		return edits[i].end > edits[j].end
	})
	out := append([]byte(nil), src...)
	lastStart := len(out) + 1
	for _, e := range edits {
		if e.end > lastStart {
			continue
		}
		next := make([]byte, 0, len(out)-(e.end-e.start)+len(e.rep))
		next = append(next, out[:e.start]...)
		next = append(next, []byte(e.rep)...)
		next = append(next, out[e.end:]...)
		out = next
		lastStart = e.start
	}
	return out
}

func addImportIfMissing(src []byte, alias, pkgPath string) []byte {
	fset := token.NewFileSet()
	af, err := parser.ParseFile(fset, "", src, parser.ParseComments)
	if err != nil {
		return src
	}
	for _, imp := range af.Imports {
		val := strings.Trim(imp.Path.Value, `"`)
		if val == pkgPath {
			return src
		}
	}
	line := alias + ` "` + pkgPath + `"`
	if alias == "" {
		line = `"` + pkgPath + `"`
	}
	for _, d := range af.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok || gd.Tok != token.IMPORT {
			continue
		}
		if gd.Lparen.IsValid() {
			off := fset.Position(gd.Lparen).Offset + 1
			return applyEdits(src, []edit{{off, off, "\n\t" + line}})
		}
		// single import: convert `import "foo"` -> `import (\n\t"foo"\n\tline\n)`
		sOff := fset.Position(gd.Pos()).Offset
		eOff := fset.Position(gd.End()).Offset
		orig := string(src[sOff:eOff])
		inner := strings.TrimSpace(strings.TrimPrefix(orig, "import"))
		rep := "import (\n\t" + inner + "\n\t" + line + "\n)"
		return applyEdits(src, []edit{{sOff, eOff, rep}})
	}
	// No import decl at all: insert after `package <name>`
	off := fset.Position(af.Name.End()).Offset
	return applyEdits(src, []edit{{off, off, "\n\nimport " + line}})
}

func fixUnusedImports(src []byte, candidates map[string]bool) []byte {
	fset := token.NewFileSet()
	af, err := parser.ParseFile(fset, "", src, parser.ParseComments)
	if err != nil {
		return src
	}
	used := map[string]bool{}
	ast.Inspect(af, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok {
				used[id.Name] = true
			}
		}
		return true
	})
	var edits []edit
	for _, imp := range af.Imports {
		p := strings.Trim(imp.Path.Value, `"`)
		name := filepath.Base(p)
		if imp.Name != nil {
			name = imp.Name.Name
		}
		if name == "_" || name == "." {
			continue
		}
		if candidates[p] && !used[name] {
			s := fset.Position(imp.Pos()).Offset
			edits = append(edits, edit{s, s, "_ "})
		}
	}
	if len(edits) == 0 {
		return src
	}
	return applyEdits(src, edits)
}

var markedBlockRe = regexp.MustCompile(`(?s)\n?// ras-anything:start [^\n]*\n.*?// ras-anything:end [^\n]*\n?`)

func stripMarkedBlock(src []byte, fid string) []byte {
	re := regexp.MustCompile(`(?s)\n?// ras-anything:start ` + regexp.QuoteMeta(fid) + `\b.*?// ras-anything:end ` + regexp.QuoteMeta(fid) + `\b[^\n]*\n?`)
	return re.ReplaceAll(src, []byte("\n"))
}

// --------------------------------------------------------------------------- instrument

type pkgRegItem struct {
	FID, File, Name, Module string
	Line                    int
	ArgOK, RetOK            bool
}

func cmdInstrument(specPath string) error {
	sb, err := os.ReadFile(specPath)
	if err != nil {
		return err
	}
	var spec InstrumentSpec
	if err := json.Unmarshal(sb, &spec); err != nil {
		return err
	}
	absWork, err := filepath.Abs(spec.WorkDir)
	if err != nil {
		return err
	}
	if spec.Module == "" {
		spec.Module, _ = readGoMod(absWork)
	}
	if spec.AgentImport == "" {
		spec.AgentImport = spec.Module + "/_ras_agent"
	}
	benchSet := map[string]bool{}
	for _, b := range spec.BenchFIDs {
		benchSet[b] = true
	}

	files, err := walkGoFiles(absWork)
	if err != nil {
		return err
	}

	fset := token.NewFileSet()
	pkgRegs := map[string][]pkgRegItem{}
	pkgNames := map[string]string{}
	var targetInfo *FnInfo

	for _, fpath := range files {
		src, err := os.ReadFile(fpath)
		if err != nil {
			return err
		}
		// In verify mode, strip any already-enabled marked block for TargetFID so we inspect cleanly
		if spec.Mode == "verify" && spec.TargetFID != "" {
			src = stripMarkedBlock(src, spec.TargetFID)
		}

		af, err := parser.ParseFile(fset, fpath, src, parser.ParseComments)
		if err != nil {
			return fmt.Errorf("parse %s: %w", fpath, err)
		}
		rel, _ := filepath.Rel(absWork, fpath)
		rel = filepath.ToSlash(rel)
		pkgRel := filepath.ToSlash(filepath.Dir(rel))
		pkgName := af.Name.Name
		pkgNames[pkgRel] = pkgName
		modPref := pkgModulePrefix(pkgRel, pkgName)

		var edits []edit
		needAgent := false
		needReflect := false

		// 1. Rewrite boundary / exit / clock calls inside AST
		ast.Inspect(af, func(n ast.Node) bool {
			sel, ok := n.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			pkgID, ok := sel.X.(*ast.Ident)
			if !ok {
				return true
			}
			s := fset.Position(sel.Pos()).Offset
			e := fset.Position(sel.End()).Offset
			switch pkgID.Name + "." + sel.Sel.Name {
			case "os.Exit":
				edits = append(edits, edit{s, e, "_ras_ag.ExitWith"})
				needAgent = true
			case "log.Fatal":
				edits = append(edits, edit{s, e, "_ras_ag.LogFatal"})
				needAgent = true
			case "log.Fatalf":
				edits = append(edits, edit{s, e, "_ras_ag.LogFatalf"})
				needAgent = true
			case "log.Fatalln":
				edits = append(edits, edit{s, e, "_ras_ag.LogFatalln"})
				needAgent = true
			case "exec.Command":
				edits = append(edits, edit{s, e, "_ras_ag.ExecCommand"})
				needAgent = true
			case "exec.CommandContext":
				edits = append(edits, edit{s, e, "_ras_ag.ExecCommandContext"})
				needAgent = true
			}
			if spec.Mode == "verify" {
				switch pkgID.Name + "." + sel.Sel.Name {
				case "time.Sleep":
					edits = append(edits, edit{s, e, "_ras_ag.Sleep"})
					needAgent = true
				case "time.Now":
					edits = append(edits, edit{s, e, "_ras_ag.Now"})
					needAgent = true
				case "time.After":
					edits = append(edits, edit{s, e, "_ras_ag.After"})
					needAgent = true
				case "time.NewTimer":
					edits = append(edits, edit{s, e, "_ras_ag.NewTimer"})
					needAgent = true
				case "context.WithTimeout":
					edits = append(edits, edit{s, e, "_ras_ag.WithTimeout"})
					needAgent = true
				case "context.WithDeadline":
					edits = append(edits, edit{s, e, "_ras_ag.WithDeadline"})
					needAgent = true
				}
			}
			return true
		})

		// 2. Wrap functions (main finish, Enter/Exit, record/bench/verify target)
		hasOrigTarget := false
		for _, decl := range af.Decls {
			if fn, ok := decl.(*ast.FuncDecl); ok && fn.Body != nil && strings.HasPrefix(fn.Name.Name, "_ras_orig_") {
				hasOrigTarget = true
			}
		}

		for _, decl := range af.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			name := fn.Name.Name
			if name == "init" {
				continue
			}
			isMain := pkgName == "main" && name == "main" && fn.Recv == nil
			if isMain {
				if spec.Mode == "verify" {
					// Rename user's main to avoid duplicate func main() if target is in main
					nmStart := fset.Position(fn.Name.Pos()).Offset
					nmEnd := fset.Position(fn.Name.End()).Offset
					edits = append(edits, edit{nmStart, nmEnd, "_ras_user_main"})
				} else {
					bodyOpen := fset.Position(fn.Body.Lbrace).Offset + 1
					edits = append(edits, edit{bodyOpen, bodyOpen, "\n\tdefer _ras_ag.Finish()"})
					needAgent = true
				}
				continue
			}

			if strings.HasPrefix(name, "_ras_") {
				// If enable already created _ras_orig_Target, capture targetInfo from it in verify mode
				if spec.Mode == "verify" && strings.HasPrefix(name, "_ras_orig_") {
					origName := strings.TrimPrefix(name, "_ras_orig_")
					fid := modPref + "." + origName
					if fid == spec.TargetFID {
						targetInfo = extractFnInfo(fn, fset, src, fid, origName, modPref, pkgName, pkgRel, rel)
					}
				}
				continue
			}

			var recvType string
			qualname := name
			if fn.Recv != nil && len(fn.Recv.List) > 0 {
				recvType = exprString(fset, fn.Recv.List[0].Type)
				qualname = baseRecvType(recvType) + "." + name
			}
			fid := modPref + "." + qualname

			line := fset.Position(fn.Pos()).Line
			pkgRegs[pkgRel] = append(pkgRegs[pkgRel], pkgRegItem{
				FID: fid, File: rel, Name: name, Module: modPref, Line: line, ArgOK: true, RetOK: true,
			})

			bodyOpen := fset.Position(fn.Body.Lbrace).Offset + 1
			if spec.Mode != "verify" {
				edits = append(edits, edit{bodyOpen, bodyOpen, fmt.Sprintf("\n\tdefer _ras_ag.Exit(_ras_ag.Enter(%q))", fid)})
				needAgent = true
			}

			// Record mode target wrapper
			if spec.Mode == "record" && fid == spec.TargetFID {
				ti := extractFnInfo(fn, fset, src, fid, name, modPref, pkgName, pkgRel, rel)
				nmStart := fset.Position(fn.Name.Pos()).Offset
				nmEnd := fset.Position(fn.Name.End()).Offset
				edits = append(edits, edit{nmStart, nmEnd, "_ras_rec_" + name})
				fnEnd := fset.Position(fn.End()).Offset
				edits = append(edits, edit{fnEnd, fnEnd, "\n\n" + genRecordWrapper(ti)})
				needAgent = true
			}

			// Bench mode target wrapper
			if spec.Mode == "bench" && benchSet[fid] {
				fbVar := ""
				safeFID := strings.ReplaceAll(strings.ReplaceAll(fid, ".", "_"), "-", "_")
				for _, d2 := range af.Decls {
					if gd, ok := d2.(*ast.GenDecl); ok {
						for _, sp := range gd.Specs {
							if vs, ok := sp.(*ast.ValueSpec); ok {
								for _, id := range vs.Names {
									if id.Name == "RAS_FALLBACK" || id.Name == "_ras_fb_"+safeFID {
										fbVar = id.Name
									}
								}
							}
						}
					}
				}
				ti := extractFnInfo(fn, fset, src, fid, name, modPref, pkgName, pkgRel, rel)
				nmStart := fset.Position(fn.Name.Pos()).Offset
				nmEnd := fset.Position(fn.Name.End()).Offset
				edits = append(edits, edit{nmStart, nmEnd, "_ras_bench_" + name})
				fnEnd := fset.Position(fn.End()).Offset
				edits = append(edits, edit{fnEnd, fnEnd, "\n\n" + genBenchWrapper(ti, fbVar)})
				needAgent = true
				if fbVar != "" {
					needReflect = true
				}
			}

			// Verify mode target rename
			if spec.Mode == "verify" && fid == spec.TargetFID && !hasOrigTarget {
				targetInfo = extractFnInfo(fn, fset, src, fid, name, modPref, pkgName, pkgRel, rel)
				nmStart := fset.Position(fn.Name.Pos()).Offset
				nmEnd := fset.Position(fn.Name.End()).Offset
				edits = append(edits, edit{nmStart, nmEnd, "_ras_orig_" + name})
			}
		}

		outSrc := applyEdits(src, edits)
		if needAgent {
			outSrc = addImportIfMissing(outSrc, "_ras_ag", spec.AgentImport)
		}
		if needReflect {
			outSrc = addImportIfMissing(outSrc, "_ras_refl", "reflect")
		}
		outSrc = fixUnusedImports(outSrc, map[string]bool{
			"os": true, "log": true, "os/exec": true, "time": true, "context": true,
		})
		if err := os.WriteFile(fpath, outSrc, 0o644); err != nil {
			return err
		}
	}

	// 3. Emit ras_register.go per package (except in verify)
	if spec.Mode != "verify" {
		for pkgRel, items := range pkgRegs {
			pkgName := pkgNames[pkgRel]
			var buf bytes.Buffer
			fmt.Fprintf(&buf, "package %s\n\nimport _ras_ag %q\n\nfunc init() {\n", pkgName, spec.AgentImport)
			for _, it := range items {
				fmt.Fprintf(&buf, "\t_ras_ag.RegisterFn(%q, %q, %d, %q, %q, %v, %v)\n",
					it.FID, it.File, it.Line, it.Name, it.Module, it.ArgOK, it.RetOK)
			}
			buf.WriteString("}\n")
			dest := filepath.Join(absWork, pkgRel, "ras_register_gen.go")
			if err := os.WriteFile(dest, buf.Bytes(), 0o644); err != nil {
				return err
			}
		}
	}

	// 4. Verify mode: emit hardened copy + bridge + _ras_verify/main.go
	if spec.Mode == "verify" {
		if targetInfo == nil {
			return fmt.Errorf("target function %q not found in %s", spec.TargetFID, absWork)
		}
		if spec.HardenedFile == "" {
			return fmt.Errorf("hardened_file required in verify mode")
		}
		if err := emitVerifyFiles(absWork, spec, targetInfo); err != nil {
			return err
		}
	}
	return nil
}

func extractFnInfo(fn *ast.FuncDecl, fset *token.FileSet, src []byte, fid, name, modPref, pkgName, pkgRel, rel string) *FnInfo {
	var params []ParamInfo
	if fn.Type.Params != nil {
		for _, field := range fn.Type.Params.List {
			ts := exprString(fset, field.Type)
			if len(field.Names) == 0 {
				params = append(params, ParamInfo{Name: fmt.Sprintf("arg%d", len(params)), Type: ts})
			} else {
				for _, nm := range field.Names {
					params = append(params, ParamInfo{Name: nm.Name, Type: ts})
				}
			}
		}
	}
	var results []ParamInfo
	if fn.Type.Results != nil {
		for _, field := range fn.Type.Results.List {
			ts := exprString(fset, field.Type)
			if len(field.Names) == 0 {
				results = append(results, ParamInfo{Name: "", Type: ts})
			} else {
				for _, nm := range field.Names {
					results = append(results, ParamInfo{Name: nm.Name, Type: ts})
				}
			}
		}
	}
	return &FnInfo{
		ID: fid, Qualname: name, Name: name, Module: modPref,
		PkgName: pkgName, PkgDir: pkgRel, File: rel,
		Params: params, Results: results,
	}
}

func sigString(ti *FnInfo) (paramList, callArgs, resList string, hasErr bool, valTypes []string) {
	var ps, args []string
	for i, p := range ti.Params {
		nm := p.Name
		if nm == "" || nm == "_" {
			nm = fmt.Sprintf("_p%d", i)
		}
		ps = append(ps, nm+" "+p.Type)
		args = append(args, nm)
	}
	var rs []string
	for _, r := range ti.Results {
		rs = append(rs, r.Type)
		if r.Type == "error" {
			hasErr = true
		} else {
			valTypes = append(valTypes, r.Type)
		}
	}
	if len(rs) > 1 {
		resList = "(" + strings.Join(rs, ", ") + ")"
	} else if len(rs) == 1 {
		resList = rs[0]
	}
	return strings.Join(ps, ", "), strings.Join(args, ", "), resList, hasErr, valTypes
}

func genRecordWrapper(ti *FnInfo) string {
	ps, args, rs, hasErr, valTypes := sigString(ti)
	var recArgs []string
	for i, p := range ti.Params {
		if p.Type == "context.Context" {
			continue
		}
		nm := p.Name
		if nm == "" || nm == "_" {
			nm = fmt.Sprintf("_p%d", i)
		}
		recArgs = append(recArgs, nm)
	}

	var b bytes.Buffer
	fmt.Fprintf(&b, "func %s(%s) %s {\n", ti.Name, ps, rs)
	// Build inner call
	var retNames []string
	for i := range valTypes {
		retNames = append(retNames, fmt.Sprintf("_v%d", i))
	}
	errVar := "nil"
	if hasErr {
		retNames = append(retNames, "_err")
		errVar = "_err"
	}
	inner := fmt.Sprintf("_ras_rec_%s(%s)", ti.Name, args)
	if len(retNames) > 0 {
		inner = strings.Join(retNames, ", ") + " := " + inner
	}
	var packVal string
	if len(valTypes) == 0 {
		packVal = "nil"
	} else if len(valTypes) == 1 {
		packVal = "_v0"
	} else {
		var vs []string
		for i := range valTypes {
			vs = append(vs, fmt.Sprintf("_v%d", i))
		}
		packVal = "[]any{" + strings.Join(vs, ", ") + "}"
	}

	fmt.Fprintf(&b, "\t_raw, _rerr := _ras_ag.RecordCall(%q, []any{%s}, func() (any, error) {\n", ti.ID, strings.Join(recArgs, ", "))
	fmt.Fprintf(&b, "\t\t%s\n", inner)
	fmt.Fprintf(&b, "\t\treturn %s, %s\n", packVal, errVar)
	fmt.Fprintf(&b, "\t})\n")

	// Unpack return
	if len(valTypes) == 0 && !hasErr {
		b.WriteString("\t_ = _raw\n\t_ = _rerr\n}\n")
		return b.String()
	}
	var outExprs []string
	if len(valTypes) == 1 {
		fmt.Fprintf(&b, "\tvar _res0 %s\n\tif _raw != nil { _res0 = _raw.(%s) }\n", valTypes[0], valTypes[0])
		outExprs = append(outExprs, "_res0")
	} else if len(valTypes) > 1 {
		fmt.Fprintf(&b, "\t_sl := _raw.([]any)\n")
		for i, vt := range valTypes {
			fmt.Fprintf(&b, "\tvar _res%d %s\n\tif _sl[%d] != nil { _res%d = _sl[%d].(%s) }\n", i, vt, i, i, i, vt)
			outExprs = append(outExprs, fmt.Sprintf("_res%d", i))
		}
	} else {
		b.WriteString("\t_ = _raw\n")
	}
	if hasErr {
		outExprs = append(outExprs, "_rerr")
	} else {
		b.WriteString("\t_ = _rerr\n")
	}
	fmt.Fprintf(&b, "\treturn %s\n}\n", strings.Join(outExprs, ", "))
	return b.String()
}

func genBenchWrapper(ti *FnInfo, fbVar string) string {
	ps, args, rs, hasErr, valTypes := sigString(ti)
	var b bytes.Buffer
	fmt.Fprintf(&b, "func %s(%s) %s {\n", ti.Name, ps, rs)
	var retNames []string
	for i := range valTypes {
		retNames = append(retNames, fmt.Sprintf("_v%d", i))
	}
	if hasErr {
		retNames = append(retNames, "_err")
	}
	for i, vt := range valTypes {
		fmt.Fprintf(&b, "\tvar _v%d %s\n", i, vt)
	}
	if hasErr {
		b.WriteString("\tvar _err error\n")
	}
	call := fmt.Sprintf("_ras_bench_%s(%s)", ti.Name, args)
	if len(retNames) > 0 {
		call = strings.Join(retNames, ", ") + " = " + call
	}
	errPass := "nil"
	if hasErr {
		errPass = "_err"
	}
	if fbVar != "" && len(valTypes) == 1 {
		fmt.Fprintf(&b, "\t%s\n\t_ = _ras_ag.TrackBench(%q, _err == nil && _ras_refl.DeepEqual(_v0, %s), func() error {\n\t\treturn %s\n\t})\n", call, ti.ID, fbVar, errPass)
	} else {
		fmt.Fprintf(&b, "\t_ = _ras_ag.TrackBench(%q, false, func() error {\n\t\t%s\n\t\treturn %s\n\t})\n", ti.ID, call, errPass)
	}
	if len(retNames) > 0 {
		fmt.Fprintf(&b, "\treturn %s\n", strings.Join(retNames, ", "))
	}
	b.WriteString("}\n")
	return b.String()
}

func emitVerifyFiles(absWork string, spec InstrumentSpec, ti *FnInfo) error {
	hb, err := os.ReadFile(spec.HardenedFile)
	if err != nil {
		return err
	}
	fset := token.NewFileSet()
	haf, err := parser.ParseFile(fset, spec.HardenedFile, hb, parser.ParseComments)
	if err != nil {
		return fmt.Errorf("parse hardened file: %w", err)
	}

	var edits []edit
	hasFallbackVar := false
	needAgent := false

	// If hardened file uses a different package name (e.g. package good), adapt to target package
	hPkg := haf.Name.Name
	targetImportPath := spec.Module
	if ti.PkgDir != "." && ti.PkgDir != "" {
		targetImportPath = spec.Module + "/" + ti.PkgDir
	}
	if hPkg != ti.PkgName {
		edits = append(edits, edit{fset.Position(haf.Name.Pos()).Offset, fset.Position(haf.Name.End()).Offset, ti.PkgName})
		for _, imp := range haf.Imports {
			if strings.Trim(imp.Path.Value, `"`) == targetImportPath {
				s := fset.Position(imp.Pos()).Offset
				e := fset.Position(imp.End()).Offset
				edits = append(edits, edit{s, e, ""})
			}
		}
		ast.Inspect(haf, func(n ast.Node) bool {
			if sel, ok := n.(*ast.SelectorExpr); ok {
				if id, ok := sel.X.(*ast.Ident); ok && id.Name == ti.PkgName {
					s := fset.Position(id.Pos()).Offset
					e := fset.Position(sel.Sel.Pos()).Offset
					edits = append(edits, edit{s, e, ""})
				}
			}
			return true
		})
	}

	ast.Inspect(haf, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.ValueSpec:
			for _, id := range x.Names {
				if id.Name == "RAS_FALLBACK" {
					hasFallbackVar = true
				}
			}
		case *ast.FuncDecl:
			if x.Name.Name == ti.Name && x.Recv == nil {
				s := fset.Position(x.Name.Pos()).Offset
				e := fset.Position(x.Name.End()).Offset
				edits = append(edits, edit{s, e, "_ras_hardened_" + ti.Name})
			}
		case *ast.SelectorExpr:
			if id, ok := x.X.(*ast.Ident); ok {
				s := fset.Position(x.Pos()).Offset
				e := fset.Position(x.End()).Offset
				switch id.Name + "." + x.Sel.Name {
				case "time.Sleep":
					edits = append(edits, edit{s, e, "_ras_ag.Sleep"})
					needAgent = true
				case "time.Now":
					edits = append(edits, edit{s, e, "_ras_ag.Now"})
					needAgent = true
				case "time.After":
					edits = append(edits, edit{s, e, "_ras_ag.After"})
					needAgent = true
				case "time.NewTimer":
					edits = append(edits, edit{s, e, "_ras_ag.NewTimer"})
					needAgent = true
				case "context.WithTimeout":
					edits = append(edits, edit{s, e, "_ras_ag.WithTimeout"})
					needAgent = true
				case "context.WithDeadline":
					edits = append(edits, edit{s, e, "_ras_ag.WithDeadline"})
					needAgent = true
				case "exec.Command":
					edits = append(edits, edit{s, e, "_ras_ag.ExecCommand"})
					needAgent = true
				case "exec.CommandContext":
					edits = append(edits, edit{s, e, "_ras_ag.ExecCommandContext"})
					needAgent = true
				}
			}
		}
		return true
	})

	hOut := applyEdits(hb, edits)
	if needAgent {
		hOut = addImportIfMissing(hOut, "_ras_ag", spec.AgentImport)
	}
	hOut = fixUnusedImports(hOut, map[string]bool{
		"time": true, "context": true, "os/exec": true,
	})
	hDest := filepath.Join(absWork, ti.PkgDir, "ras_verify_hardened_gen.go")
	if err := os.WriteFile(hDest, hOut, 0o644); err != nil {
		return err
	}

	// Build ras_verify_bridge_gen.go inside ti.PkgDir
	var b bytes.Buffer
	needJSON := false
	for _, p := range ti.Params {
		if p.Type != "context.Context" {
			needJSON = true
			break
		}
	}
	hasRuntime := false
	rtPath := filepath.Join(absWork, ti.PkgDir, "_ras_hardened", "runtime.go")
	if _, err := os.Stat(rtPath); err == nil && spec.RuntimeImport != "" {
		hasRuntime = true
	}

	fmt.Fprintf(&b, "package %s\n\nimport (\n", ti.PkgName)
	b.WriteString("\t\"context\"\n")
	if needJSON {
		b.WriteString("\t\"encoding/json\"\n")
	}
	if hasFallbackVar {
		b.WriteString("\t\"reflect\"\n")
	}
	fmt.Fprintf(&b, "\t_ras_ag %q\n", spec.AgentImport)
	if hasRuntime {
		fmt.Fprintf(&b, "\t_ras_rt %q\n", spec.RuntimeImport)
	}
	b.WriteString(")\n\n")
	b.WriteString("var _ = context.Background\n")

	b.WriteString("func RasVerifyEntry(jobPath string) {\n")
	if hasRuntime {
		b.WriteString("\t_ras_rt.SetClockHooks(_ras_ag.Sleep, _ras_ag.Now)\n")
	}
	fbExpr := "nil"
	if hasFallbackVar {
		fbExpr = "RAS_FALLBACK"
	}
	resetExpr := "nil"
	if hasRuntime {
		resetExpr = "_ras_rt.ResetAll"
	}
	fmt.Fprintf(&b, "\t_ras_ag.RunVerify(jobPath, %s, %s, func(side string, _ int, rawArgs []json.RawMessage) (any, error, bool) {\n", fbExpr, resetExpr)

	// Wait: if !needJSON, we still need encoding/json in the signature of RunVerify callback
	// Let's always import encoding/json cleanly!
	var argVars []string
	rawIdx := 0
	for i, p := range ti.Params {
		if p.Type == "context.Context" {
			argVars = append(argVars, "context.Background()")
			continue
		}
		v := fmt.Sprintf("_a%d", i)
		fmt.Fprintf(&b, "\t\tvar %s %s\n", v, p.Type)
		fmt.Fprintf(&b, "\t\tif %d < len(rawArgs) { _ = json.Unmarshal(rawArgs[%d], &%s) }\n", rawIdx, rawIdx, v)
		argVars = append(argVars, v)
		rawIdx++
	}

	_, _, _, hasErr, valTypes := sigString(ti)
	var retNames []string
	for i := range valTypes {
		retNames = append(retNames, fmt.Sprintf("_r%d", i))
	}
	if hasErr {
		retNames = append(retNames, "_err")
	}
	for i, vt := range valTypes {
		fmt.Fprintf(&b, "\t\tvar _r%d %s\n", i, vt)
	}
	if hasErr {
		b.WriteString("\t\tvar _err error\n")
	}

	callArgs := strings.Join(argVars, ", ")
	lhs := ""
	if len(retNames) > 0 {
		lhs = strings.Join(retNames, ", ") + " = "
	}
	fmt.Fprintf(&b, "\t\tif side == \"original\" {\n\t\t\t%s_ras_orig_%s(%s)\n\t\t} else {\n\t\t\t%s_ras_hardened_%s(%s)\n\t\t}\n",
		lhs, ti.Name, callArgs, lhs, ti.Name, callArgs)

	var packVal string
	if len(valTypes) == 0 {
		packVal = "nil"
	} else if len(valTypes) == 1 {
		packVal = "_r0"
	} else {
		var vs []string
		for i := range valTypes {
			vs = append(vs, fmt.Sprintf("_r%d", i))
		}
		packVal = "[]any{" + strings.Join(vs, ", ") + "}"
	}
	errOut := "nil"
	if hasErr {
		errOut = "_err"
	}
	if hasFallbackVar && len(valTypes) == 1 {
		fmt.Fprintf(&b, "\t\t_isFB := _err == nil && reflect.DeepEqual(_r0, RAS_FALLBACK)\n")
		fmt.Fprintf(&b, "\t\treturn %s, %s, _isFB\n", packVal, errOut)
	} else {
		fmt.Fprintf(&b, "\t\treturn %s, %s, false\n", packVal, errOut)
	}
	b.WriteString("\t})\n}\n")

	bridgeSrc := b.String()
	if !needJSON {
		bridgeSrc = strings.Replace(bridgeSrc, "\"context\"\n", "\"context\"\n\t\"encoding/json\"\n", 1)
	}
	if err := os.WriteFile(filepath.Join(absWork, ti.PkgDir, "ras_verify_bridge_gen.go"), []byte(bridgeSrc), 0o644); err != nil {
		return err
	}

	// Emit _ras_verify/main.go (or main package wrapper if ti.PkgName == "main")
	if ti.PkgName == "main" {
		mainSrc := fmt.Sprintf("package main\n\nimport \"os\"\n\nfunc main() {\n\tRasVerifyEntry(os.Args[1])\n}\n")
		return os.WriteFile(filepath.Join(absWork, ti.PkgDir, "ras_verify_main_gen.go"), []byte(mainSrc), 0o644)
	}
	vDir := filepath.Join(absWork, "_ras_verify")
	if err := os.MkdirAll(vDir, 0o755); err != nil {
		return err
	}
	mainSrc := fmt.Sprintf("package main\n\nimport (\n\t\"os\"\n\ttarget %q\n)\n\nfunc main() {\n\ttarget.RasVerifyEntry(os.Args[1])\n}\n", targetImportPath)
	return os.WriteFile(filepath.Join(vDir, "main.go"), []byte(mainSrc), 0o644)
}


type HardenedImport struct {
	Alias string `json:"alias"`
	Path  string `json:"path"`
}

type ExtractOut struct {
	Imports       []HardenedImport `json:"imports"`
	DeclsSrc      string           `json:"decls_src"`
	HardenedFnSrc string           `json:"hardened_fn_src"`
	HasFallback   bool             `json:"has_fallback"`
	Forbidden     []string         `json:"forbidden"`
}

func cmdExtractHardened(path, targetName, targetPkg string) error {
	src, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	fset := token.NewFileSet()
	af, err := parser.ParseFile(fset, path, src, parser.ParseComments)
	if err != nil {
		return err
	}
	var out ExtractOut
	for _, imp := range af.Imports {
		ip := strings.Trim(imp.Path.Value, `"`)
		al := ""
		if imp.Name != nil {
			al = imp.Name.Name
		}
		if strings.HasSuffix(ip, "/_ras_agent") || ip == "unsafe" || ip == "plugin" {
			out.Forbidden = append(out.Forbidden, ip)
		}
		if strings.HasSuffix(ip, "/_ras_hardened") {
			continue
		}
		out.Imports = append(out.Imports, HardenedImport{Alias: al, Path: ip})
	}
	var declChunks []string
	for _, decl := range af.Decls {
		if gd, ok := decl.(*ast.GenDecl); ok && gd.Tok == token.IMPORT {
			continue
		}
		if fn, ok := decl.(*ast.FuncDecl); ok && fn.Name.Name == targetName && fn.Recv == nil {
			nmS := fset.Position(fn.Name.Pos()).Offset
			nmE := fset.Position(fn.Name.End()).Offset
			fnS := fset.Position(fn.Pos()).Offset
			fnE := fset.Position(fn.End()).Offset
			out.HardenedFnSrc = string(src[fnS:nmS]) + "_ras_hardened_" + targetName + string(src[nmE:fnE])
			continue
		}
		if gd, ok := decl.(*ast.GenDecl); ok {
			for _, sp := range gd.Specs {
				if vs, ok := sp.(*ast.ValueSpec); ok {
					for _, id := range vs.Names {
						if id.Name == "RAS_FALLBACK" {
							out.HasFallback = true
						}
					}
				}
			}
		}
		dS := fset.Position(decl.Pos()).Offset
		dE := fset.Position(decl.End()).Offset
		declChunks = append(declChunks, string(src[dS:dE]))
	}
	out.DeclsSrc = strings.Join(declChunks, "\n\n")
	if out.Imports == nil {
		out.Imports = []HardenedImport{}
	}
	if out.Forbidden == nil {
		out.Forbidden = []string{}
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "usage: asttool <inspect|instrument|extract-hardened> <path>")
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "inspect":
		err = cmdInspect(os.Args[2])
	case "instrument":
		err = cmdInstrument(os.Args[2])
	case "extract-hardened":
		if len(os.Args) < 5 {
			fmt.Fprintln(os.Stderr, "usage: asttool extract-hardened <file> <target_name> <target_pkg>")
			os.Exit(2)
		}
		err = cmdExtractHardened(os.Args[2], os.Args[3], os.Args[4])
	default:
		err = fmt.Errorf("unknown subcommand %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
