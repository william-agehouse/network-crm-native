import Foundation
import SwiftUI

// What an answer's text becomes on screen: the engine's canned lines rewritten the way the
// web page rewrites them (ask.js:104-183), the "(AI chat was unavailable: …)" suffix lifted
// out into a quiet notice, and the markdown-lite reply turned into paragraphs, lists and
// inline bold/italic/code/links.

/// A notice above an answer: what happened, optionally one action, and the raw cause
/// behind "Show detail".
struct AskNoticeContent: Equatable {
    enum Tone { case warn, bad }
    var tone: Tone = .warn
    var text: String
    /// The answer used keyword search because of a setting in Network CRM.
    var offersSettings = false
    var detail: String = ""
}

enum AskReply {
    /// Rewrites at RENDER time, so older stored replies get the new wording too.
    // from static/app/ask.js:104-117
    private static let serverCopy: [(NSRegularExpression, String)] = [
        (re(#"^\s*here.?s who i found[^\n]*"#), ""),
        (re(#"^\s*i couldn.?t find anyone matching that[^\n]*"#), "No contact matches that question. Use other words."),
        (re(#"^\s*i didn.?t find anything for that\.?"#), "No contact matches that question. Use other words."),
        (re(#"^\s*that question needed too many steps[^\n]*"#), "This question needs too many steps. Split it into two questions."),
    ]

    // from static/app/ask.js:121
    private static let aiDown = re(#"\s*\(AI chat was unavailable:\s*([\s\S]*?)\)\s*$"#)

    private static func re(_ p: String) -> NSRegularExpression {
        // Patterns are literals checked by the build's own run; a failure here is a typo.
        try! NSRegularExpression(pattern: p, options: [.caseInsensitive])
    }

    /// The text to read and, when the AI path failed, the notice that explains it.
    // from static/app/ask.js:172-183
    static func normalize(_ raw: String) -> (text: String, notice: AskNoticeContent?) {
        var text = raw
        var notice: AskNoticeContent?
        let whole = NSRange(text.startIndex..., in: text)
        if let m = aiDown.firstMatch(in: text, range: whole), let r = Range(m.range(at: 1), in: text) {
            notice = aiDownNotice(String(text[r]))
            text = aiDown.stringByReplacingMatches(in: text, range: whole, withTemplate: "")
        }
        for (pattern, replacement) in serverCopy {
            // JS String.replace with a non-global regex: the first match only.
            let range = NSRange(text.startIndex..., in: text)
            if let m = pattern.firstMatch(in: text, range: range), let r = Range(m.range, in: text) {
                text.replaceSubrange(r, with: replacement)
            }
        }
        return (text.trimmingCharacters(in: .whitespacesAndNewlines), notice)
    }

    /// Names the cause of a failed AI call and the one action that repairs it.
    // from static/app/ask.js:131-168
    static func aiDownNotice(_ detail: String) -> AskNoticeContent {
        let d = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        func has(_ p: String) -> Bool { d.range(of: p, options: [.regularExpression, .caseInsensitive]) != nil }
        if has(#"no module named|modulenotfound|import error|importerror"#) {
            return .init(
                text: "The anthropic package is not installed. Ask used keyword search for this answer.",
                offersSettings: true, detail: d)
        }
        if has(#"credit balance|\b402\b|billing|purchase credits|quota"#) {
            return .init(
                text: "The Anthropic account is out of credit. Ask used keyword search for this answer. Top up at console.anthropic.com and ask again.",
                detail: d)
        }
        if has(#"authentication|invalid.{0,3}api.{0,3}key|x-api-key|unauthorized|\b401\b|\b403\b"#) {
            return .init(
                text: "The Anthropic API rejected the key. Ask used keyword search for this answer.",
                offersSettings: true, detail: d)
        }
        if has(#"rate.?limit|\b429\b|overloaded|\b529\b"#) {
            return .init(
                text: "The Anthropic API is busy. Ask used keyword search for this answer. Send the question again in one minute.",
                detail: d)
        }
        return .init(text: "Ask cannot reach the Anthropic API. Ask used keyword search for this answer.", detail: d)
    }
}

// MARK: - Markdown-lite

/// The reply's blocks. The web page (ui.js:264-278) only makes a list of a block whose
/// EVERY line is a bullet, so "Intro:\n- a\n- b" shows the dashes as text; here bullets are
/// recognised line by line, so a list right under its lead-in line still draws as a list.
enum AskBlock: Equatable {
    case paragraph([String])
    case bullets([String])
    case numbered([(String, String)])
    case heading(String)

    static func == (a: AskBlock, b: AskBlock) -> Bool {
        switch (a, b) {
        case (.paragraph(let x), .paragraph(let y)), (.bullets(let x), .bullets(let y)): x == y
        case (.heading(let x), .heading(let y)): x == y
        case (.numbered(let x), .numbered(let y)): x.map(\.0) == y.map(\.0) && x.map(\.1) == y.map(\.1)
        default: false
        }
    }
}

enum AskMarkdown {
    private static let bullet = try! NSRegularExpression(pattern: #"^\s*[-•*]\s+(.*)$"#)
    private static let numbered = try! NSRegularExpression(pattern: #"^\s*(\d{1,3})[.)]\s+(.*)$"#)
    private static let heading = try! NSRegularExpression(pattern: #"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$"#)

    private static func capture(_ re: NSRegularExpression, _ line: String, _ group: Int) -> String? {
        let r = NSRange(line.startIndex..., in: line)
        guard let m = re.firstMatch(in: line, range: r), let g = Range(m.range(at: group), in: line) else { return nil }
        return String(line[g])
    }

    static func blocks(_ text: String) -> [AskBlock] {
        var out: [AskBlock] = []
        var para: [String] = []
        var bullets: [String] = []
        var numbers: [(String, String)] = []
        func flush() {
            if !para.isEmpty { out.append(.paragraph(para)); para = [] }
            if !bullets.isEmpty { out.append(.bullets(bullets)); bullets = [] }
            if !numbers.isEmpty { out.append(.numbered(numbers)); numbers = [] }
        }
        for raw in text.components(separatedBy: "\n") {
            let line = raw.replacingOccurrences(of: "\r", with: "")
            if line.trimmingCharacters(in: .whitespaces).isEmpty {
                flush()
                continue
            }
            if let h = capture(heading, line, 1) {
                flush()
                out.append(.heading(h))
            } else if let b = capture(bullet, line, 1), !line.trimmingCharacters(in: .whitespaces).hasPrefix("**") {
                if !para.isEmpty || !numbers.isEmpty { flush() }
                bullets.append(b)
            } else if let n = capture(numbered, line, 1), let t = capture(numbered, line, 2) {
                if !para.isEmpty || !bullets.isEmpty { flush() }
                numbers.append((n, t))
            } else {
                if !bullets.isEmpty || !numbers.isEmpty { flush() }
                para.append(line.trimmingCharacters(in: .whitespaces))
            }
        }
        flush()
        return out
    }

    private static let linkDetector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)

    /// **bold**, *italic*, `code` and [text](url) through Foundation's markdown parser,
    /// plus bare http(s) links, which that parser leaves as text.
    static func inline(_ s: String) -> AttributedString {
        var a =
            (try? AttributedString(
                markdown: s,
                options: .init(allowsExtendedAttributes: false, interpretedSyntax: .inlineOnlyPreservingWhitespace,
                    failurePolicy: .returnPartiallyParsedIfPossible))) ?? AttributedString(s)
        guard let detector = linkDetector else { return a }
        let plain = String(a.characters)
        for m in detector.matches(in: plain, range: NSRange(plain.startIndex..., in: plain)) {
            guard let url = m.url, let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
                let r = Range(m.range, in: plain)
            else { continue }
            let lower = plain.distance(from: plain.startIndex, to: r.lowerBound)
            let length = plain.distance(from: r.lowerBound, to: r.upperBound)
            let start = a.characters.index(a.characters.startIndex, offsetBy: lower)
            let end = a.characters.index(start, offsetBy: length)
            if a[start..<end].link == nil { a[start..<end].link = url }
        }
        return a
    }
}

/// The reply drawn as blocks. Selectable, so a sentence can be copied out of it.
struct AskMarkdownView: View {
    let text: String

    var body: some View {
        let blocks = AskMarkdown.blocks(text)
        VStack(alignment: .leading, spacing: 9) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .font(.system(size: Metrics.fsUI))
        .foregroundStyle(Palette.ink)
        .lineSpacing(3)
        .textSelection(.enabled)
        .tint(Palette.accentInk)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func view(for block: AskBlock) -> some View {
        switch block {
        case .heading(let t):
            Text(AskMarkdown.inline(t)).fontWeight(.semibold).padding(.top, 2)
        case .paragraph(let lines):
            Text(AskMarkdown.inline(lines.joined(separator: "\n")))
                .fixedSize(horizontal: false, vertical: true)
        case .bullets(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text("•").foregroundStyle(Palette.muted)
                        Text(AskMarkdown.inline(item)).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.leading, 4)
        case .numbered(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("\(item.0).").monospacedDigit().foregroundStyle(Palette.muted)
                            .frame(minWidth: 16, alignment: .trailing)
                        Text(AskMarkdown.inline(item.1)).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.leading, 2)
        }
    }
}
