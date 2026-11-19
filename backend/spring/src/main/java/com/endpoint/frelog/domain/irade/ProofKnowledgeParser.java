package com.endpoint.frelog.domain.irade;

import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@Component
public class ProofKnowledgeParser {

    public record ParsedDefinition(
            String symbolName,
            String definitionKind,
            String signatureText,
            String sourceCode,
            String contentHash
    ) {
    }

    public record ParsedTheorem(
            String theoremName,
            String statementKind,
            String statementText,
            String proofCode,
            String proofBody,
            String sourceHash
    ) {
    }

    public record ParsedProofBundle(
            List<ParsedDefinition> definitions,
            List<ParsedTheorem> theorems
    ) {
    }

    private static final Pattern LEAN_DECL_START = Pattern.compile(
            "(?m)^[ \\t]*(?:@\\[[^\\]]*\\]\\s*)*(?:(?:private|protected|noncomputable|unsafe|partial)\\s+)*(def|abbrev|inductive|structure|class|axiom|opaque|theorem|lemma|corollary|proposition)\\s+([A-Za-z_][A-Za-z0-9_.']*)"
    );

    private static final Pattern ROCQ_DECL_START = Pattern.compile(
            "(?m)^[ \\t]*(?:Local\\s+|Global\\s+|Program\\s+)?(Definition|Fixpoint|CoFixpoint|Inductive|CoInductive|Record|Class|Axiom|Parameter|Theorem|Lemma|Corollary|Proposition|Remark|Fact)\\s+([A-Za-z_][A-Za-z0-9_']*)"
    );

    private static final Set<String> DEFINITION_KEYWORDS = Set.of(
            "def", "abbrev", "inductive", "structure", "class", "axiom", "opaque",
            "Definition", "Fixpoint", "CoFixpoint", "Inductive", "CoInductive", "Record", "Class", "Axiom", "Parameter"
    );

    private static final Set<String> LEAN_SKIP_TOKENS = Set.of(
            "rfl", "simp", "simpa", "omega", "ring", "linarith", "decide", "trivial",
            "assumption", "constructor", "cases", "induction", "intro", "intros",
            "rw", "apply", "exact", "have", "let", "show", "by", "using", "with", "at"
    );

    public ParsedProofBundle parse(String proverEngine, String sourceCode) {
        if (sourceCode == null || sourceCode.isBlank()) {
            return new ParsedProofBundle(List.of(), List.of());
        }
        String normalizedEngine = normalizeEngine(proverEngine);
        String stripped = "ROCQ".equals(normalizedEngine)
                ? stripRocqComments(sourceCode)
                : stripLeanComments(sourceCode);

        Pattern declPattern = "ROCQ".equals(normalizedEngine) ? ROCQ_DECL_START : LEAN_DECL_START;
        Matcher matcher = declPattern.matcher(stripped);

        record DeclSpan(String keyword, String name, int start, int headerEnd) {
        }

        List<DeclSpan> spans = new ArrayList<>();
        while (matcher.find()) {
            spans.add(new DeclSpan(matcher.group(1), matcher.group(2), matcher.start(), matcher.end()));
        }

        List<ParsedDefinition> definitions = new ArrayList<>();
        List<ParsedTheorem> theorems = new ArrayList<>();

        for (int i = 0; i < spans.size(); i++) {
            DeclSpan cur = spans.get(i);
            int end = (i + 1 < spans.size()) ? spans.get(i + 1).start() : stripped.length();
            String block = trimTrailingCommands(normalizedEngine, stripped.substring(cur.start(), end)).trim();
            if (block.isEmpty()) continue;

            if (DEFINITION_KEYWORDS.contains(cur.keyword())) {
                String sig = extractSignature(normalizedEngine, block, cur.keyword());
                definitions.add(new ParsedDefinition(
                        truncate(cur.name(), 190),
                        truncate(cur.keyword(), 32),
                        truncate(sig, 1000),
                        block,
                        sha256Hex(block)
                ));
            } else {
                String statementKind = toStatementKind(cur.keyword());
                StatementAndBody split = splitStatementAndBody(normalizedEngine, block);
                theorems.add(new ParsedTheorem(
                        truncate(cur.name(), 190),
                        statementKind,
                        truncate(split.statement(), 1000),
                        block,
                        split.body(),
                        sha256Hex(block)
                ));
            }
        }

        return new ParsedProofBundle(definitions, theorems);
    }

    public boolean containsIncompleteProofMarker(String proverEngine, String sourceCode) {
        if (sourceCode == null) return true;
        String stripped = "ROCQ".equals(normalizeEngine(proverEngine))
                ? stripRocqComments(sourceCode)
                : stripLeanComments(sourceCode);
        return Pattern.compile("(?<![A-Za-z0-9_])(sorry|admit|Admitted|Abort)(?![A-Za-z0-9_])")
                .matcher(stripped)
                .find();
    }

    public Optional<String> detectUsageRole(String symbolName, String statementText, String proofBody) {
        boolean inStmt = containsIdentifier(statementText, symbolName);
        boolean inProof = containsIdentifier(proofBody, symbolName);
        if (inStmt && inProof) return Optional.of("BOTH");
        if (inStmt) return Optional.of("IN_STATEMENT");
        if (inProof) return Optional.of("IN_PROOF");
        return Optional.empty();
    }

    public boolean containsIdentifier(String text, String identifier) {
        if (text == null || text.isBlank() || identifier == null || identifier.isBlank()) return false;
        Pattern p = Pattern.compile("(?<![A-Za-z0-9_'])" + Pattern.quote(identifier) + "(?![A-Za-z0-9_'])");
        return p.matcher(text).find();
    }

    public String classifyLemmaRelationType(String text, String lemmaName) {
        if (text == null || lemmaName == null) return "USES_LEMMA";
        String quoted = Pattern.quote(lemmaName);
        if (Pattern.compile("\\b(?:rw|simp|simpa|rewrite|autorewrite)\\b[^.\\n;]{0,80}(?<![A-Za-z0-9_'])" + quoted + "(?![A-Za-z0-9_'])").matcher(text).find()) {
            return "REWRITES_WITH";
        }
        if (Pattern.compile("\\b(?:apply|exact|eapply|refine|using)\\b[^.\\n;]{0,80}(?<![A-Za-z0-9_'])" + quoted + "(?![A-Za-z0-9_'])").matcher(text).find()) {
            return "APPLIES";
        }
        return "USES_LEMMA";
    }

    public List<String> extractQualifiedReferences(String proverEngine, String proofBody) {
        if (proofBody == null || proofBody.isBlank()) return List.of();
        LinkedHashSet<String> found = new LinkedHashSet<>();
        Matcher qualified = Pattern.compile("(?<![A-Za-z0-9_'])([A-Z][A-Za-z0-9_]*\\.[A-Za-z0-9_.']+)(?![A-Za-z0-9_'])").matcher(proofBody);
        while (qualified.find()) {
            String token = qualified.group(1);
            if (!LEAN_SKIP_TOKENS.contains(token)) {
                found.add(token);
            }
        }
        return new ArrayList<>(found);
    }

    public String extractReferenceContext(String text, String identifier) {
        if (text == null || identifier == null) return null;
        String[] lines = text.split("\\R");
        for (String line : lines) {
            if (containsIdentifier(line, identifier)) {
                return truncate(line.trim(), 500);
            }
        }
        return null;
    }

    public String normalizeEngine(String proverEngine) {
        if (proverEngine == null) return "LEAN4";
        String upper = proverEngine.trim().toUpperCase(Locale.ROOT);
        if ("COQ".equals(upper) || "ROCQ".equals(upper)) return "ROCQ";
        return "LEAN4";
    }

    public String sha256Hex(String content) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest((content == null ? "" : content).getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(digest.length * 2);
            for (byte b : digest) {
                sb.append(Character.forDigit((b >>> 4) & 0xF, 16));
                sb.append(Character.forDigit(b & 0xF, 16));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException("SHA-256 unavailable", ex);
        }
    }

    private record StatementAndBody(String statement, String body) {
    }

    private StatementAndBody splitStatementAndBody(String proverEngine, String block) {
        if ("ROCQ".equals(proverEngine)) {
            int dot = findRocqSentenceEnd(block);
            if (dot > 0) {
                String stmt = collapseSpaces(block.substring(0, dot));
                String body = block.substring(dot + 1).trim();
                return new StatementAndBody(stmt, body);
            }
            return new StatementAndBody(collapseSpaces(firstNonEmptyLine(block)), block);
        }
        int assign = findLeanTopLevelAssign(block);
        if (assign > 0) {
            String stmt = collapseSpaces(block.substring(0, assign));
            String body = block.substring(assign + 2).trim();
            return new StatementAndBody(stmt, body);
        }
        return new StatementAndBody(collapseSpaces(firstNonEmptyLine(block)), block);
    }

    private String extractSignature(String proverEngine, String block, String keyword) {
        if ("ROCQ".equals(proverEngine)) {
            int dot = findRocqSentenceEnd(block);
            if (dot > 0) {
                int colonEq = findLeanTopLevelAssign(block.substring(0, dot));
                if (colonEq > 0) return collapseSpaces(block.substring(0, colonEq));
                return collapseSpaces(block.substring(0, dot));
            }
            return collapseSpaces(firstNonEmptyLine(block));
        }
        int colonEq = findLeanTopLevelAssign(block);
        if (colonEq > 0) {
            return collapseSpaces(block.substring(0, colonEq));
        }
        return collapseSpaces(firstNonEmptyLine(block));
    }

    private String trimTrailingCommands(String proverEngine, String rawBlock) {
        if ("ROCQ".equals(proverEngine)) {
            Matcher qed = Pattern.compile("(?m)^(?:Qed|Defined|Save)\\s*\\.").matcher(rawBlock);
            int lastEnd = -1;
            while (qed.find()) {
                lastEnd = qed.end();
            }
            if (lastEnd > 0) {
                return rawBlock.substring(0, lastEnd);
            }
            int firstDot = findRocqSentenceEnd(rawBlock);
            return firstDot > 0 ? rawBlock.substring(0, firstDot + 1) : rawBlock;
        }
        String[] lines = rawBlock.split("\\R");
        StringBuilder sb = new StringBuilder();
        for (String line : lines) {
            String trimmed = line.trim();
            if (trimmed.startsWith("#")) break;
            if (sb.length() > 0) sb.append('\n');
            sb.append(line);
        }
        return sb.toString();
    }

    private int findLeanTopLevelAssign(String code) {
        int paren = 0;
        int brace = 0;
        int bracket = 0;
        boolean inString = false;
        for (int i = 0; i < code.length() - 1; i++) {
            char c = code.charAt(i);
            if (c == '"' && (i == 0 || code.charAt(i - 1) != '\\')) {
                inString = !inString;
                continue;
            }
            if (inString) continue;
            if (c == '(') paren++;
            else if (c == ')' && paren > 0) paren--;
            else if (c == '{') brace++;
            else if (c == '}' && brace > 0) brace--;
            else if (c == '[') bracket++;
            else if (c == ']' && bracket > 0) bracket--;
            else if (c == ':' && code.charAt(i + 1) == '=' && paren == 0 && brace == 0 && bracket == 0) {
                return i;
            }
        }
        return -1;
    }

    private int findRocqSentenceEnd(String code) {
        boolean inString = false;
        for (int i = 0; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == '"' && (i == 0 || code.charAt(i - 1) != '\\')) {
                inString = !inString;
                continue;
            }
            if (inString) continue;
            if (c == '.') {
                boolean followedBySpaceOrEnd = (i + 1 == code.length()) || Character.isWhitespace(code.charAt(i + 1));
                if (followedBySpaceOrEnd) return i;
            }
        }
        return -1;
    }

    private String stripLeanComments(String input) {
        StringBuilder out = new StringBuilder(input.length());
        int blockDepth = 0;
        boolean lineComment = false;
        for (int i = 0; i < input.length(); i++) {
            char c = input.charAt(i);
            char next = (i + 1 < input.length()) ? input.charAt(i + 1) : '\0';
            if (lineComment) {
                if (c == '\n') {
                    lineComment = false;
                    out.append('\n');
                } else {
                    out.append(' ');
                }
                continue;
            }
            if (blockDepth > 0) {
                if (c == '/' && next == '-') {
                    blockDepth++;
                    out.append("  ");
                    i++;
                } else if (c == '-' && next == '/') {
                    blockDepth--;
                    out.append("  ");
                    i++;
                } else {
                    out.append(c == '\n' ? '\n' : ' ');
                }
                continue;
            }
            if (c == '-' && next == '-') {
                lineComment = true;
                out.append("  ");
                i++;
            } else if (c == '/' && next == '-') {
                blockDepth = 1;
                out.append("  ");
                i++;
            } else {
                out.append(c);
            }
        }
        return out.toString();
    }

    private String stripRocqComments(String input) {
        StringBuilder out = new StringBuilder(input.length());
        int blockDepth = 0;
        for (int i = 0; i < input.length(); i++) {
            char c = input.charAt(i);
            char next = (i + 1 < input.length()) ? input.charAt(i + 1) : '\0';
            if (blockDepth > 0) {
                if (c == '(' && next == '*') {
                    blockDepth++;
                    out.append("  ");
                    i++;
                } else if (c == '*' && next == ')') {
                    blockDepth--;
                    out.append("  ");
                    i++;
                } else {
                    out.append(c == '\n' ? '\n' : ' ');
                }
                continue;
            }
            if (c == '(' && next == '*') {
                blockDepth = 1;
                out.append("  ");
                i++;
            } else {
                out.append(c);
            }
        }
        return out.toString();
    }

    private String toStatementKind(String keyword) {
        return switch (keyword.toLowerCase(Locale.ROOT)) {
            case "lemma" -> "LEMMA";
            case "corollary" -> "COROLLARY";
            case "proposition", "remark", "fact" -> "PROPOSITION";
            case "example" -> "EXAMPLE";
            default -> "THEOREM";
        };
    }

    private String firstNonEmptyLine(String block) {
        for (String line : block.split("\\R")) {
            if (!line.isBlank()) return line;
        }
        return block;
    }

    private String collapseSpaces(String text) {
        return text == null ? "" : text.trim().replaceAll("\\s+", " ");
    }

    private String truncate(String value, int maxLen) {
        if (value == null) return "";
        return value.length() <= maxLen ? value : value.substring(0, maxLen);
    }
}
