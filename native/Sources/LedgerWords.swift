import Foundation

// The words the Data and Settings pages work out themselves, copied rule for rule from the
// web page so both interfaces say the same thing. Pure functions, so --selftest can check
// them without an engine.

enum LedgerWords {
    /// NC-327: the one box that can cost money says how to put a ceiling on it.
    // from static/app/ledger.js ANTHROPIC_SPEND_HINT
    static let anthropicSpendHint = "Anthropic bills your own account for every question, draft and summary. To put a ceiling on that, set a monthly spending limit on Anthropic's site, in your account's Settings, under Limits."

    /// "just now", "12 minutes ago", "an hour ago", "3 hours ago", "yesterday", "5 days ago".
    /// The seconds come from the engine, which knows the stamp's time zone. "" for nil.
    // from static/app/data.js:36-45
    static func ago(_ seconds: Double?) -> String {
        guard let s = seconds, s.isFinite else { return "" }
        if s < 90 { return "just now" }
        let m = Int((s / 60).rounded(.toNearestOrAwayFromZero))
        let h = Int((s / 3600).rounded(.toNearestOrAwayFromZero))
        let d = Int((s / 86400).rounded(.toNearestOrAwayFromZero))
        if s < 3600 { return "\(m) minutes ago" }
        if s < 36 * 3600 { return h == 1 ? "an hour ago" : "\(h) hours ago" }
        if d < 2 { return "yesterday" }
        return "\(d) days ago"
    }

    /// What an open row's "Now" block says about when it last ran (NC-217).
    // from static/app/data.js:50-54
    static func lastRun(_ d: LedgerDetail) -> String {
        guard !d.lastRun.isEmpty else { return "" }
        let words = ago(d.lastRunSeconds)
        return "Last imported \(words.isEmpty ? Display.relDate(String(d.lastRun.prefix(10))) : words)."
    }

    /// Server sentences are written to stand alone, some without a full stop; the "Now"
    /// block runs several together.
    // from static/app/data.js:61
    static func stop(_ s: String) -> String {
        guard let last = s.last else { return s }
        return ".!?".contains(last) ? s : s + "."
    }

    /// "1.2 GB", "340 MB", "12 KB" (never "0 KB").
    // from static/app/data.js:63-68
    static func size(_ bytes: Int) -> String {
        let n = Double(max(0, bytes))
        if n >= 1e9 { return String(format: "%.1f GB", n / 1e9) }
        if n >= 1e6 { return "\(Display.number(Int((n / 1e6).rounded(.toNearestOrAwayFromZero)))) MB" }
        return "\(Display.number(max(1, Int((n / 1e3).rounded(.toNearestOrAwayFromZero))))) KB"
    }

    /// "1 record", "3 records".
    // from static/app/data.js:73
    static func plural(_ n: Int, _ word: String) -> String { "\(n) \(word)\(n == 1 ? "" : "s")" }

    /// A count is never a bare 0 or a blank: nothing yet, cannot be counted, or the number.
    // from static/app/data.js:107-110
    static func provenanceCount(_ k: DataProvenanceKind) -> String {
        guard let c = k.count else { return "Not known" }
        return c == 0 ? "None yet" : Display.number(c)
    }

    /// The first character upper-cased, as the web page does for a server sentence.
    static func capitalised(_ s: String) -> String { s.prefix(1).uppercased() + s.dropFirst() }

    // MARK: Figures

    /// The words for a unit, one and many (crm/figures.py UNITS, for the units the pages print).
    // from static/app/ledger.js:211-215
    static func unitWords(_ unit: String) -> (String, String) {
        switch unit {
        case "people": ("person", "people")
        case "messages": ("message", "messages")
        case "photos": ("photo", "photos")
        case "meetings": ("meeting", "meetings")
        case "sources": ("source", "sources")
        case "profiles": ("profile", "profiles")
        default: (unit, unit)
        }
    }

    /// A number that says what it counts: the bold lead ("1,250 people", "12 of 40 people"),
    /// the words after it, and the hover text naming the group it counts. nil when the
    /// figure has no population (the web page draws nothing then).
    // from static/app/ledger.js:221-236
    static func figure(_ fig: LedgerFigure?, words: String = "", whole: Bool = true)
        -> (lead: String, tail: String, help: String)?
    {
        guard let fig, !fig.population.isEmpty else { return nil }
        let (one, many) = unitWords(fig.unit)
        let n = Display.number(fig.n)
        let lead: String
        if whole, let of = fig.of {
            lead = "\(n) of \(Display.number(of)) \(of == 1 ? one : many)"
        } else {
            lead = "\(n) \(fig.n == 1 ? one : many)"
        }
        let tail = words.isEmpty ? fig.says : words
        return (lead, tail, "Counting \(fig.populationSays)")
    }

    // MARK: Tones

    /// The words for a tone, said aloud and on hover.
    // from static/app/ledger.js:116-119
    static func tone(_ tone: String) -> String {
        switch tone {
        case "ok": "Working"
        case "off": "Off"
        case "behind": "Falling behind"
        case "blocked": "Stopped"
        case "paused": "Paused"
        case "na": "Not in this copy"
        case "ready": "Waiting for you"
        default: tone
        }
    }

    // MARK: A Data row's "Now" block and second line

    /// A state worth a second line under "You have now": anything but plain working or
    /// plain off, whose dot already says it.
    // from static/app/data.js:347-350 (and settings.js:321-322)
    static func note(_ row: LedgerRow) -> String {
        ["blocked", "behind", "paused", "na", "ready"].contains(row.state.tone) ? row.state.says : ""
    }

    /// How often a source runs, said only when it is true: a row that is off must not
    /// describe itself as running, nor say one thing twice (NC-217).
    // from static/app/data.js:351-360
    static func runs(_ row: LedgerRow) -> String {
        let r = row.detail.runs
        if r.isEmpty || r == row.state.says { return "" }
        let working = row.inUse && row.state.tone != "paused"
        if working || r.hasPrefix("Only when") { return r }
        if !row.state.says.isEmpty { return "" }
        return "Once it is on: " + r.prefix(1).lowercased() + r.dropFirst()
    }

    /// The "Now" block of an open Data row, as one paragraph.
    // from static/app/data.js:367-368
    static func now(_ row: LedgerRow) -> String {
        [row.inUse ? row.state.says : (row.state.says.isEmpty ? "Off" : row.state.says),
         runs(row), lastRun(row.detail), row.inUse ? row.detail.lastRunCaveat : ""]
            .filter { !$0.isEmpty }.map(stop).joined(separator: " ")
    }

    /// Setup steps fold away on a row that works, so what it added is not pushed below
    /// the fold every time (NC-217).
    // from static/app/data.js:363-366
    static func foldsHelp(_ row: LedgerRow) -> Bool { row.inUse && row.state.tone == "ok" }

    /// One of the three automatic passes, as one sentence run.
    // from static/app/data.js:324-329
    static func part(_ p: LedgerPart) -> String {
        [p.inUse ? p.state.says : (p.state.says.isEmpty ? "Not yet" : p.state.says),
         p.lastRunSeconds != nil ? "Last ran \(ago(p.lastRunSeconds))" : "", p.have]
            .filter { !$0.isEmpty }.map(stop).joined(separator: " ")
    }

    // MARK: Possible duplicates

    /// The line beside "Possible duplicates".
    // from static/app/data.js:447-449
    static func duplicates(_ count: Int?) -> String {
        guard let count else { return "Could not be checked just now" }
        if count == 0 { return "None found" }
        return "\(Display.number(count)) \(count == 1 ? "group" : "groups") of people who may be the same person"
    }

    /// The record to keep by default: the most filled (company, role type, place, blurb and
    /// each topic count one), then the most messages, so a bare row cannot silently win
    /// over the enriched one and lose its facts.
    // from static/app/data.js:193-197
    static func bestToKeep(_ group: [DataDupMember]) -> Int? {
        func fill(_ m: DataDupMember) -> Int {
            [m.company, m.roleType, m.homeMetro, m.blurb].filter { !$0.isEmpty }.count + m.topics.count
        }
        // A stable sort, like JS Array.sort: ties keep the engine's order.
        let ranked = group.enumerated().sorted { a, b in
            let fa = fill(a.element), fb = fill(b.element)
            if fa != fb { return fa > fb }
            let ma = a.element.counts.total, mb = b.element.counts.total
            if ma != mb { return ma > mb }
            return a.offset < b.offset
        }
        return ranked.first?.element.id ?? group.first?.id
    }

    /// The facts under a name in a duplicates group.
    // from static/app/data.js:209
    static func dupMeta(_ m: DataDupMember) -> String {
        [m.roleType, m.homeMetro, m.company, m.topics.prefix(3).joined(separator: ", "), m.source,
         m.last.isEmpty ? "" : "last " + Display.relDate(m.last)]
            .filter { !$0.isEmpty }.joined(separator: " · ")
    }

    // MARK: Help text

    enum HelpBlock: Hashable {
        case steps([String])
        case paragraph(String)
        case bullets([String])
    }

    /// A source's own instructions laid out as what they are: numbered lines become an
    /// ordered list, one instruction per step; everything else stays prose. The words are
    /// the engine's (importers.REGISTRY); this changes the layout, not a word.
    // from static/app/ledger.js:76-91 and static/app/ui.js:264-276 (Md)
    static func helpBlocks(_ text: String) -> [HelpBlock] {
        let blocks = text.components(separatedBy: "\n").reduce(into: [[String]]([[]])) { acc, line in
            if line.trimmingCharacters(in: .whitespaces).isEmpty {
                if !(acc.last?.isEmpty ?? true) { acc.append([]) }
            } else {
                acc[acc.count - 1].append(line)
            }
        }.filter { !$0.isEmpty }
        return blocks.map { lines in
            if lines.allSatisfy({ $0.range(of: #"^\s*\d+\.\s+"#, options: .regularExpression) != nil }) {
                return .steps(lines.map { $0.replacingOccurrences(of: #"^\s*\d+\.\s+"#, with: "", options: .regularExpression) })
            }
            if lines.allSatisfy({ $0.range(of: #"^\s*[-•*]\s+"#, options: .regularExpression) != nil }) {
                return .bullets(lines.map { $0.replacingOccurrences(of: #"^\s*[-•*]\s+"#, with: "", options: .regularExpression) })
            }
            return .paragraph(lines.joined(separator: "\n"))
        }
    }

    /// Markdown-lite (bold, italic, code, links) as an attributed string; plain text if it
    /// does not parse.
    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

// MARK: - What a pasted value must look like before it is saved

/// The engine checks none of these, so the box is the one place (NC-218).
enum LedgerFormat {
    private static func matches(_ v: String, _ pattern: String) -> Bool {
        v.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    // from static/app/ledger.js:253-264
    static func check(_ key: String, _ v: String) -> String {
        switch key {
        case "SELF_EMAIL":
            // NC-328: one address, or several separated by commas.
            let parts = v.split(whereSeparator: { ",; \t\n".contains($0) }).map(String.init)
            return !parts.isEmpty && parts.allSatisfy({ matches($0, #"^[^@\s]+@[^@\s]+\.[^@\s]+$"#) })
                ? "" : "Type whole email addresses, like you@example.com. Put a comma between two."
        case "ANTHROPIC_API_KEY":
            return v.hasPrefix("sk-ant-") ? "" : "Keys from Anthropic start with sk-ant-. Copy the whole key again."
        case "NC_ICS_GOOGLE_URL", "NC_ICS_ICLOUD_URL":
            // NC-303: several addresses, saved with one comma between.
            return v.split(separator: ",").allSatisfy({ matches(String($0), #"^(https|webcals?)://\S+$"#) })
                ? "" : "Start each address with https:// or webcal://. Copy it again from the calendar's settings."
        case "NC_DATES_LOOKAHEAD_DAYS":
            if let n = Int(v), v.allSatisfy(\.isNumber), (1...365).contains(n) { return "" }
            return "Type a whole number of days, from 1 to 365."
        default:
            return ""
        }
    }

    static func hasFormat(_ key: String) -> Bool {
        ["SELF_EMAIL", "ANTHROPIC_API_KEY", "NC_ICS_GOOGLE_URL", "NC_ICS_ICLOUD_URL", "NC_DATES_LOOKAHEAD_DAYS"].contains(key)
    }

    /// What is saved for a pasted value. On a calendar row, whatever separates
    /// two addresses as pasted becomes one comma (NC-303).
    // from static/app/ledger.js pastedValue
    static func pasted(_ key: String, _ value: String) -> String {
        let v = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ["NC_ICS_GOOGLE_URL", "NC_ICS_ICLOUD_URL"].contains(key) else { return v }
        return v.split(whereSeparator: { $0.isWhitespace || $0 == "," || $0 == ";" }).joined(separator: ",")
    }

    /// Why a pasted secret is refused before it is sent, or "".
    // from static/app/ledger.js secretProblem
    static func secretProblem(_ value: String, masked: String = "", key: String = "") -> String {
        if value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "Paste it first." }
        if value.contains("-----BEGIN") { return "That is a private key. Paste the token itself, never a key file." }
        let trimmed = pasted(key, value)
        if trimmed.contains(where: { $0.isWhitespace }) {
            return "That has a space or a line break in it. Copy the value again, on its own."
        }
        if !masked.isEmpty && trimmed == masked { return "That is the hidden copy shown here. Paste the real value." }
        return check(key, trimmed)
    }

    /// Why a plain setting is refused before it is sent, or "". An empty box is Clear.
    // from static/app/ledger.js:343-350
    static func textProblem(_ key: String, _ value: String) -> String {
        if key == "KEY" && value.contains("-----BEGIN") {
            return "That is the key itself. Paste where the key file is instead, like ~/.ssh/id_ed25519."
        }
        let v = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? "" : check(key, v)
    }
}
