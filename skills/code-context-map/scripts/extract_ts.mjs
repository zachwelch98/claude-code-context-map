#!/usr/bin/env node
// Syntax-only TypeScript symbol extractor for the code-context-map skill.
//
// Uses the real TypeScript parser (`ts.createSourceFile`, no type-checking, no tsconfig), so
// signatures are exact rather than regex-guessed. Called by extractors.extract_typescript.
//
//   stdin : {"typescript": "<abs path to a typescript package dir>", "files": ["<abs path>", ...]}
//   stdout: {"<abs path>": {"ok": true, "symbols": [...]} | {"ok": false, "error": "..."}}
//
// Symbols use the same shape as extractors.Symbol. Variable initializers are reported as a coarse
// `init` descriptor (never raw object/array/call text) so the Python side can redact secrets.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const ts = createRequire(import.meta.url)(input.typescript);
if (typeof ts.createSourceFile !== "function") {
  console.error("this typescript package has no JS compiler API");
  process.exit(2);
}
const MF = ts.ModifierFlags;

const squash = (s, limit) => {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length > limit ? t.slice(0, limit) + "…" : t;
};

function extractFile(file) {
  const text = fs.readFileSync(file, "utf8");
  const scriptKind = path.extname(file).toLowerCase() === ".tsx" ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, scriptKind);

  // Mirror the Python extractor: a file that doesn't parse is a failure, so the caller leaves any
  // previously good map entry alone instead of overwriting it with a half-parsed one.
  const diag = (sf.parseDiagnostics || [])[0];
  if (diag) {
    const { line } = sf.getLineAndCharacterOfPosition(diag.start || 0);
    throw new Error(`syntax error line ${line + 1}: ${ts.flattenDiagnosticMessageText(diag.messageText, " ")}`);
  }

  const symbols = [];
  const lineOf = (node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
  const src = (node, limit) => squash(node.getText(sf), limit);

  function jsdoc(node) {
    const docs = node.jsDoc;
    if (!docs || !docs.length) return null;
    const c = docs[docs.length - 1].comment;
    const t = typeof c === "string" ? c : c ? ts.getTextOfJSDocComment(c) || "" : "";
    const first = t.trim().split("\n")[0].trim();
    return first ? squash(first, 100) : null;
  }

  function modifiers(node) {
    const f = ts.getCombinedModifierFlags(node);
    const out = [];
    if (f & MF.Export) out.push("export");
    if (f & MF.Default) out.push("default");
    if (f & MF.Ambient) out.push("declare");
    if (f & MF.Abstract) out.push("abstract");
    if (f & MF.Private) out.push("private");
    else if (f & MF.Protected) out.push("protected");
    if (f & MF.Static) out.push("static");
    if (f & MF.Async) out.push("async");
    return out.length ? out : null;
  }

  const typeParams = (node) =>
    node.typeParameters && node.typeParameters.length
      ? squash(`<${node.typeParameters.map((t) => t.getText(sf)).join(", ")}>`, 120)
      : null;

  function param(p) {
    const info = {
      name: ts.isIdentifier(p.name) ? p.name.text : src(p.name, 60),
      default: p.initializer ? src(p.initializer, 80) : null,
      kind: p.dotDotDotToken ? "vararg" : "positional",
      annotation: p.type ? src(p.type, 120) : null,
    };
    if (p.questionToken) info.optional = true;
    if (ts.getCombinedModifierFlags(p) & (MF.Public | MF.Private | MF.Protected | MF.Readonly)) info.property = true;
    return info;
  }

  function calleeName(e) {
    if (ts.isIdentifier(e)) return e.text;
    if (ts.isPropertyAccessExpression(e)) return e.name.text;
    return null;
  }

  function collectCalls(node) {
    const calls = [];
    const visit = (n) => {
      if (ts.isCallExpression(n) || ts.isNewExpression(n)) {
        const name = calleeName(n.expression);
        if (name && name !== "require") {
          const start = n.getStart(sf);
          calls.push({ name, raw: squash(text.slice(start, start + 400), 120), line: lineOf(n) });
        }
      }
      ts.forEachChild(n, visit);
    };
    visit(node);
    return calls.length ? calls : null;
  }

  // fn: the function-like node; decl: the node carrying modifiers/JSDoc (differs for `const f = () => {}`).
  function functionSymbol({ fn, decl, docNode, name, owner, ownerName, typedAs }) {
    let doc = jsdoc(docNode || decl);
    if (!doc && typedAs) doc = `typed: ${squash(typedAs.getText(sf), 80)}`;
    return {
      kind: owner === "class" ? "method" : "function",
      name,
      line: lineOf(decl),
      params: fn.parameters.filter((p) => !(ts.isIdentifier(p.name) && p.name.text === "this")).map(param),
      return_type: fn.type ? src(fn.type, 120) : null,
      class_context: ownerName || null,
      doc,
      precise: true,
      calls: fn.body ? collectCalls(fn) : null,
      generics: typeParams(fn),
      modifiers: modifiers(decl),
    };
  }

  // Overload signatures after the first are real (callers see them) but are one definition, not
  // several: tag them so the Python side doesn't count them as "ambiguous" name collisions.
  function tagOverload(sym, decl, seen) {
    if (decl.body) return sym;
    const key = `${sym.class_context || ""}.${sym.name}`;
    if (seen.has(key)) sym.modifiers = [...(sym.modifiers || []), "overload"];
    seen.add(key);
    return sym;
  }

  const memberName = (m) => {
    if (ts.isConstructorDeclaration(m)) return "constructor";
    const n = m.name;
    return n && (ts.isIdentifier(n) || ts.isStringLiteral(n) || ts.isPrivateIdentifier(n) || ts.isNumericLiteral(n))
      ? n.text
      : n
        ? src(n, 60)
        : "<anonymous>";
  };

  const unwrap = (e) => {
    while (
      e &&
      (ts.isParenthesizedExpression(e) ||
        ts.isAsExpression(e) ||
        ts.isNonNullExpression(e) ||
        (ts.isSatisfiesExpression && ts.isSatisfiesExpression(e)))
    ) {
      e = e.expression;
    }
    return e;
  };
  const isFunctionValue = (e) => e && (ts.isArrowFunction(e) || ts.isFunctionExpression(e));

  function initInfo(e) {
    if (!e) return null;
    if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return { kind: "string", text: src(e, 80), value: e.text };
    if (
      ts.isNumericLiteral(e) ||
      e.kind === ts.SyntaxKind.TrueKeyword ||
      e.kind === ts.SyntaxKind.FalseKeyword ||
      e.kind === ts.SyntaxKind.NullKeyword ||
      (ts.isPrefixUnaryExpression(e) && ts.isNumericLiteral(e.operand))
    ) {
      return { kind: "literal", text: src(e, 80) };
    }
    if (ts.isArrayLiteralExpression(e)) return { kind: "array" };
    if (ts.isObjectLiteralExpression(e)) return { kind: "object" };
    if (ts.isCallExpression(e) || ts.isNewExpression(e)) return { kind: "call", callee: src(e.expression, 60) };
    return { kind: "expr" };
  }

  const requireSpecifier = (e) => {
    e = unwrap(e);
    return e && ts.isCallExpression(e) && ts.isIdentifier(e.expression) && e.expression.text === "require" &&
      e.arguments.length === 1 && ts.isStringLiteralLike(e.arguments[0])
      ? e.arguments[0].text
      : null;
  };

  const pushImport = (node, spec, doc) =>
    symbols.push({ kind: "import", name: spec, line: lineOf(node), doc, module: spec, precise: true });

  function visitClass(st, name) {
    symbols.push({
      kind: "class", name, line: lineOf(st), doc: jsdoc(st), precise: true,
      generics: typeParams(st), modifiers: modifiers(st),
      heritage: st.heritageClauses && st.heritageClauses.length
        ? squash(st.heritageClauses.map((h) => h.getText(sf)).join(" "), 120)
        : null,
    });
    // Callers only see overload signatures, never the implementation's own signature.
    const overloaded = new Set(
      st.members.filter((m) => ts.isMethodDeclaration(m) && !m.body).map(memberName),
    );
    const seenSignatures = new Set();
    for (const m of st.members) {
      const mname = memberName(m);
      if (ts.isConstructorDeclaration(m) || ts.isMethodDeclaration(m)) {
        if (m.body && overloaded.has(mname)) continue; // implementation of an overloaded method
        symbols.push(tagOverload(
          functionSymbol({ fn: m, decl: m, name: mname, owner: "class", ownerName: name }), m, seenSignatures,
        ));
      } else if (ts.isGetAccessorDeclaration(m) || ts.isSetAccessorDeclaration(m)) {
        const sym = functionSymbol({ fn: m, decl: m, name: mname, owner: "class", ownerName: name });
        sym.doc = sym.doc || (ts.isGetAccessorDeclaration(m) ? "getter" : "setter");
        symbols.push(sym);
      } else if (ts.isPropertyDeclaration(m) && isFunctionValue(unwrap(m.initializer))) {
        symbols.push(functionSymbol({
          fn: unwrap(m.initializer), decl: m, name: mname, owner: "class", ownerName: name, typedAs: m.type,
        }));
      }
    }
  }

  function visitInterface(st) {
    const name = st.name.text;
    symbols.push({
      kind: "interface", name, line: lineOf(st), doc: jsdoc(st), precise: true,
      generics: typeParams(st), modifiers: modifiers(st),
      heritage: st.heritageClauses && st.heritageClauses.length
        ? squash(st.heritageClauses.map((h) => h.getText(sf)).join(" "), 120)
        : null,
    });
    for (const m of st.members) {
      if (ts.isMethodSignature(m)) {
        symbols.push(functionSymbol({ fn: m, decl: m, name: memberName(m), owner: "class", ownerName: name }));
      } else if (ts.isPropertySignature(m)) {
        symbols.push({
          kind: "variable", name: memberName(m), line: lineOf(m), class_context: name,
          return_type: m.type ? src(m.type, 120) : null, doc: jsdoc(m), precise: true,
          modifiers: m.questionToken ? ["optional"] : null,
        });
      }
    }
  }

  function visitVariableStatement(st) {
    for (const d of st.declarationList.declarations) {
      if (!ts.isIdentifier(d.name)) continue; // destructuring: no single name to map
      const name = d.name.text;
      const init = unwrap(d.initializer);
      const spec = requireSpecifier(d.initializer);
      if (isFunctionValue(init)) {
        symbols.push(functionSymbol({ fn: init, decl: d, docNode: st, name, owner: null, typedAs: d.type }));
      } else if (spec) {
        pushImport(d, spec, `${name} = require(...)`);
      } else {
        symbols.push({
          kind: "variable", name, line: lineOf(d), return_type: d.type ? src(d.type, 120) : null,
          doc: jsdoc(st), precise: true, init: initInfo(init), modifiers: modifiers(d),
        });
      }
    }
  }

  function visitStatements(stmts, ns) {
    const overloaded = new Set(
      stmts.filter((s) => ts.isFunctionDeclaration(s) && !s.body && s.name).map((s) => s.name.text),
    );
    const seenSignatures = new Set();
    for (const st of stmts) {
      if (ts.isFunctionDeclaration(st)) {
        const name = st.name ? st.name.text : "default";
        if (st.body && overloaded.has(name)) continue; // implementation of an overloaded function
        symbols.push(tagOverload(
          functionSymbol({ fn: st, decl: st, name, owner: ns ? "namespace" : null, ownerName: ns }), st, seenSignatures,
        ));
      } else if (ts.isClassDeclaration(st)) {
        visitClass(st, st.name ? st.name.text : "default");
      } else if (ts.isInterfaceDeclaration(st)) {
        visitInterface(st);
      } else if (ts.isTypeAliasDeclaration(st)) {
        symbols.push({
          kind: "type", name: st.name.text, line: lineOf(st), doc: src(st.type, 120), precise: true,
          generics: typeParams(st), modifiers: modifiers(st),
        });
      } else if (ts.isEnumDeclaration(st)) {
        symbols.push({
          kind: "enum", name: st.name.text, line: lineOf(st), precise: true, modifiers: modifiers(st),
          doc: squash(st.members.map((m) => memberName(m)).join(", "), 100),
        });
      } else if (ts.isVariableStatement(st)) {
        visitVariableStatement(st);
      } else if (ts.isImportDeclaration(st)) {
        pushImport(st, st.moduleSpecifier.text, src(st, 100));
      } else if (ts.isImportEqualsDeclaration(st)) {
        const ref = st.moduleReference;
        if (ts.isExternalModuleReference(ref) && ts.isStringLiteralLike(ref.expression)) {
          pushImport(st, ref.expression.text, src(st, 100));
        }
      } else if (ts.isExportDeclaration(st)) {
        if (st.moduleSpecifier) pushImport(st, st.moduleSpecifier.text, src(st, 100));
      } else if (ts.isExportAssignment(st)) {
        const e = unwrap(st.expression);
        if (isFunctionValue(e)) {
          const sym = functionSymbol({ fn: e, decl: st, name: "default", owner: null });
          sym.modifiers = ["export", "default"];
          symbols.push(sym);
        }
      } else if (ts.isModuleDeclaration(st)) {
        let body = st.body;
        const parts = [st.name.text !== undefined ? st.name.text : st.name.getText(sf)];
        while (body && ts.isModuleDeclaration(body)) { // namespace a.b.c
          parts.push(body.name.text);
          body = body.body;
        }
        if (body && ts.isModuleBlock(body)) visitStatements(body.statements, parts.join("."));
      }
    }
  }

  visitStatements(sf.statements, null);
  return symbols;
}

const out = {};
for (const file of input.files) {
  try {
    out[file] = { ok: true, symbols: extractFile(file) };
  } catch (err) {
    out[file] = { ok: false, error: String((err && err.message) || err) };
  }
}
process.stdout.write(JSON.stringify(out));
