import AppKit
import SwiftUI

// Colours and the web page's display rules, so the native screens look and read the same.
// Colours mirror static/css/tokens.css (light `:root`, dark `[data-theme="dark"]`); every
// rule carries the file and line it was copied from. Contract §8 has the full list.

// MARK: - Colours

enum Palette {
    /// A colour that follows the window's light or dark appearance, like the CSS tokens do.
    static func dynamic(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return NSColor(hex: isDark ? dark : light)
        })
    }

    // Surfaces — from static/css/tokens.css:78-80, dark :227-229
    static let bg = dynamic(0xF3F3F4, 0x0F0F13)
    static let panel = dynamic(0xFFFFFF, 0x17171C)
    static let panel2 = dynamic(0xFAFAFB, 0x1B1B21)

    // Ink ramp — from static/css/tokens.css:85-101, dark :231-236
    static let ink = dynamic(0x17181A, 0xE9EAEC)
    static let muted = dynamic(0x6D6E73, 0x9A9CA3)
    /// Decoration only (separators, disabled hints), never text that carries meaning.
    static let faint = dynamic(0x9B9DA1, 0x6E7078)
    static let line = dynamic(0xE8E9EB, 0x2E2E38)
    static let lineStrong = dynamic(0xD7D9DD, 0x3D3E4A)
    static let hover = dynamic(0xF1F2F4, 0x21212A)

    // Actions are ink, not colour — from static/css/tokens.css:110-112, dark :240-242
    static let action = dynamic(0x1C1D21, 0xEDEDF0)
    static let actionHover = dynamic(0x34363B, 0xFFFFFF)
    static let onAction = dynamic(0xFFFFFF, 0x17181A)

    // The interaction signal (focus, selection, info) — from static/css/tokens.css:120-123, dark :244-247
    static let accent = dynamic(0x3B6FE0, 0x5B8CF0)
    static let accentInk = dynamic(0x2F59B4, 0x7EA6F5)
    static let accentSoft = dynamic(0xDCE9F8, 0x1A2540)

    // Semantic status — from static/css/tokens.css:126-132, dark :249-255
    static let green = dynamic(0x2F8A5B, 0x4FB583)
    static let greenSoft = dynamic(0xDFF2DE, 0x16281F)
    static let gold = dynamic(0x9A7B18, 0xC9A94E)
    static let goldSoft = dynamic(0xFBF0D2, 0x2A2413)
    static let red = dynamic(0xC04434, 0xE0705D)
    static let redSoft = dynamic(0xFBE3D0, 0x331B17)
    static let danger = dynamic(0xC04434, 0xE0705D)
}

extension NSColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }
}

/// Type sizes: hierarchy comes from weight, not size — from static/css/tokens.css:50-53
enum Metrics {
    static let fsOverline: CGFloat = 11
    static let fsUI: CGFloat = 13
    static let fsTitle: CGFloat = 15
    static let fsPage: CGFloat = 24
    // from static/css/tokens.css:62-68
    static let rowHeight: CGFloat = 40
    static let gap: CGFloat = 12
    static let gapSmall: CGFloat = 8
    static let padView: CGFloat = 20
    static let controlHeight: CGFloat = 28
    // from static/css/tokens.css:44-46
    static let radius: CGFloat = 12
    static let radiusSmall: CGFloat = 8
    static let radiusPill: CGFloat = 4
}

// MARK: - Dates

enum Display {
    /// Engine timestamps are naive UTC; days are counted from noon UTC of the date part,
    /// like the web page, so a time of day never tips the count.
    private static let utc = TimeZone(identifier: "UTC")!

    private static func noonUTC(_ s: String) -> Date? {
        let day = String(s.prefix(10))
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        var c = DateComponents()
        c.year = parts[0]; c.month = parts[1]; c.day = parts[2]; c.hour = 12
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = utc
        guard let d = cal.date(from: c), cal.component(.day, from: d) == parts[2] else { return nil }
        return d
    }

    private static func roundedDays(from d: Date, to now: Date) -> Int {
        // JS Math.round rounds .5 up, Swift's .rounded() rounds half away from zero: same for >= 0.
        Int((now.timeIntervalSince(d) / 86400).rounded(.toNearestOrAwayFromZero))
    }

    /// "today", "yesterday", "3d ago", "2mo ago", "1.4y ago", "3y ago". Never weeks, never
    /// future phrasing. "" for "", the first ten characters when unparseable.
    // from static/app/api.js:221-231
    static func relDate(_ s: String, now: Date = Date()) -> String {
        guard !s.isEmpty else { return "" }
        guard let d = noonUTC(s) else { return String(s.prefix(10)) }
        let days = roundedDays(from: d, to: now)
        if days <= 0 { return "today" }
        if days == 1 { return "yesterday" }
        if days < 30 { return "\(days)d ago" }
        if days < 365 { return "\(Int((Double(days) / 30).rounded(.toNearestOrAwayFromZero)))mo ago" }
        let years = Double(days) / 365
        return days < 730 ? String(format: "%.1fy ago", years) : "\(Int(years.rounded(.toNearestOrAwayFromZero)))y ago"
    }

    /// Days since a timestamp, clamped at 0. nil = never (or unparseable) — NOT the same as 0.
    // from static/app/people.js:63-68
    static func daysSince(_ s: String, now: Date = Date()) -> Int? {
        guard !s.isEmpty, let d = noonUTC(s) else { return nil }
        return max(0, roundedDays(from: d, to: now))
    }

    /// The "Coming up" chip: "today", "tomorrow", "in 5 days".
    // from static/app/people.js:596-600
    static func whenShort(_ days: Int) -> String {
        if days <= 0 { return "today" }
        if days == 1 { return "tomorrow" }
        return "in \(days) days"
    }

    /// "Oct 14, 2026" in the user's locale, read in UTC so the day never shifts.
    // from static/app/api.js:240-245
    static func fmtDay(_ s: String) -> String {
        guard !s.isEmpty else { return "" }
        guard let d = noonUTC(s) else { return String(s.prefix(10)) }
        var style = Date.FormatStyle(date: .abbreviated, time: .omitted)
        style.timeZone = utc
        return d.formatted(style)
    }

    /// "Oct 2026", for the Connected / First contact tile. The month as written, in UTC.
    // from static/app/api.js:233-238
    static func fmtMonthYear(_ s: String) -> String {
        guard !s.isEmpty else { return "" }
        guard let d = noonUTC(s) else { return String(s.prefix(10)) }
        var style = Date.FormatStyle().month(.abbreviated).year()
        style.timeZone = utc
        return d.formatted(style)
    }

    /// A raw engine timestamp shown as text in a message bubble: "2026-10-14 16:08", UTC.
    // from static/app/people.js:1861
    static func bubbleTime(_ ts: String) -> String { String(ts.prefix(16)) }

    /// The Ask greeting by local hour, with the first word of SELF_NAME when set.
    // from static/app/ask.js:60-66
    static func greeting(selfName: String?, now: Date = Date()) -> String {
        let h = Calendar.current.component(.hour, from: now)
        let part = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"
        let first = (selfName ?? "").split(whereSeparator: \.isWhitespace).first.map(String.init) ?? ""
        return first.isEmpty ? "Good \(part)" : "Good \(part), \(first)"
    }

    // MARK: Staleness

    /// How long since last contact, as a colour on the text. Silence has to have a colour.
    enum Staleness: String, Sendable {
        case fresh, warm, cool, cold

        // from static/css/tokens.css:154-161, dark :261-264
        var color: Color {
            switch self {
            case .fresh: Palette.dynamic(0x2B7D53, 0x4FB583)
            case .warm: Palette.dynamic(0x866B15, 0xC9A94E)
            case .cool: Palette.dynamic(0x9E5F28, 0xD08A4E)
            case .cold: Palette.dynamic(0x6C6F73, 0x868890)
            }
        }
    }

    // from static/app/people.js:73-80
    static func staleness(lastContact s: String, now: Date = Date()) -> Staleness {
        guard let d = daysSince(s, now: now) else { return .cold }
        if d < 14 { return .fresh }
        if d < 90 { return .warm }
        if d < 365 { return .cool }
        return .cold
    }

    /// The "Last contact" cell: relDate, or the muted "No contact yet".
    // from static/app/people.js:229 (contract §8.6)
    static func lastContactText(_ s: String, now: Date = Date()) -> String {
        s.isEmpty ? "No contact yet" : relDate(s, now: now)
    }

    // MARK: Closeness

    enum Tier: String, CaseIterable, Sendable {
        case close, active, loose, faint

        /// The engine sends the lower-case word; the table shows it capitalised.
        // from static/app/people.js:37
        var label: String { rawValue.prefix(1).uppercased() + rawValue.dropFirst() }

        // from static/css/tokens.css:181-184, dark :280-283
        var color: Color {
            switch self {
            case .close: Palette.dynamic(0x2F8A5B, 0x4FB583)
            case .active: Palette.dynamic(0x3B6FE0, 0x5B8CF0)
            case .loose: Palette.dynamic(0x9A7B18, 0xC9A94E)
            // NC-373: was 0xD7D9DD / 0x3D3E4A, 1.4 and 1.7 to 1 against the panel, so the
            // quietest ring could not be seen. Now at least 3 to 1 on every surface.
            case .faint: Palette.dynamic(0x85878C, 0x73757D)
            }
        }

        /// A missing or unknown label counts as faint.
        // from static/app/api.js:247-248
        init(label: String) { self = Tier(rawValue: label.lowercased()) ?? .faint }

        /// The engine's own bands, for when only a number is at hand.
        // from crm/db.py:1838 (strength_label)
        init(strength: Int) {
            self = strength >= 65 ? .close : strength >= 40 ? .active : strength >= 18 ? .loose : .faint
        }
    }

    /// Avatar ring fill, 0.06...1: a score never draws as an empty ring.
    // from static/app/api.js:247-252
    static func ringFraction(strength: Int) -> Double { Double(min(100, max(6, strength))) / 100 }

    // from static/app/ui.js:203-207
    static func ringTooltip(strength: Int, label: String) -> String {
        if strength == 0 && label.isEmpty { return "No closeness score yet" }
        return "Closeness \(strength) of 100 — \(Tier(label: label).rawValue)"
    }

    /// The table's closeness cell says "No messages" instead of a score when every channel
    /// count is zero, even when notes earned the person points.
    // from static/app/people.js:533-540
    static func showsNoMessages(_ counts: ChannelCounts) -> Bool { counts.total == 0 }

    // MARK: Names and avatars

    /// Up to two initials from the first two words, "?" when empty. Uses whole Characters,
    /// so a name that starts with an emoji or flag keeps it intact (the web page splits it).
    // from static/app/api.js:199-201
    static func initials(_ name: String) -> String {
        let words = name.split(whereSeparator: \.isWhitespace).prefix(2)
        let s = words.compactMap { $0.first.map { String($0).uppercased() } }.joined()
        return s.isEmpty ? "?" : s
    }

    /// The avatar's hue pair, hashed over Unicode scalars (code points) like the web page.
    // from static/app/api.js:204-216
    static func avatarHues(_ name: String) -> (Int, Int) {
        var h = 0
        for cp in name.unicodeScalars { h = (h * 31 + Int(cp.value)) % 360 }
        return (h, (h + 40 + (h % 31)) % 360)
    }

    /// Gradient top, gradient bottom and initials colour for a monogram avatar.
    // from static/css/tokens.css:191-194, dark :291-294; static/css/ui.css:430-441
    static func avatarColors(_ name: String) -> (top: Color, bottom: Color, ink: Color) {
        let (h1, h2) = avatarHues(name)
        func hsl(_ h: Int, light: (Double, Double), dark: (Double, Double)) -> Color {
            Color(nsColor: NSColor(name: nil) { appearance in
                let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
                let (s, l) = isDark ? dark : light
                return NSColor.hsl(Double(h), s, l)
            })
        }
        return (
            hsl(h1, light: (0.22, 0.92), dark: (0.20, 0.26)),
            hsl(h2, light: (0.22, 0.87), dark: (0.20, 0.20)),
            hsl(h1, light: (0.22, 0.38), dark: (0.20, 0.80))
        )
    }

    /// "position · company", either part optional, "" when both are empty.
    // from static/app/api.js:218-220
    static func roleString(position: String, company: String) -> String {
        [position, company].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// Location cell: metro, else city.
    // from static/app/people.js:440
    static func locationString(metro: String, city: String) -> String { metro.isEmpty ? city : metro }

    // MARK: Channels

    // from static/app/api.js:180-184
    static func channelLabel(_ c: Channel) -> String {
        switch c {
        case .linkedin: "LinkedIn"
        case .imessage: "iMessage"
        case .whatsapp: "WhatsApp"
        case .email: "Email"
        case .telegram: "Telegram"
        case .messenger: "Messenger"
        case .instagram: "Instagram"
        }
    }

    /// Label for a channel string from the engine; an unknown one is shown as sent.
    static func channelLabel(_ raw: String) -> String {
        Channel(rawValue: raw).map { channelLabel($0) } ?? raw
    }

    /// imessage/whatsapp and email/linkedin are too close to tell apart by colour alone:
    /// always print the channel name beside the dot.
    // from static/css/tokens.css:165-171, dark :266-272
    static func channelColor(_ c: Channel) -> Color {
        switch c {
        case .linkedin: Palette.dynamic(0x2F6FB4, 0x5B9BE0)
        case .imessage: Palette.dynamic(0x2F8A5B, 0x4FB583)
        case .whatsapp: Palette.dynamic(0x1F8A70, 0x3FB59B)
        case .email: Palette.dynamic(0x6B5BD2, 0x9184E8)
        case .telegram: Palette.dynamic(0x2B8CCC, 0x5BB6E8)
        case .messenger: Palette.dynamic(0x7A5BD2, 0xA184E8)
        case .instagram: Palette.dynamic(0xC0447E, 0xE0709E)
        }
    }

    /// SF Symbols stand in for the web page's channel glyphs (static/app/icons.js:139-142),
    /// which are brand marks SF Symbols does not carry.
    static func channelSymbol(_ c: Channel) -> String {
        switch c {
        case .linkedin: "briefcase"
        case .imessage: "message"
        case .whatsapp: "phone.bubble"
        case .email: "envelope"
        case .telegram: "paperplane"
        case .messenger: "bubble.left.and.bubble.right"
        case .instagram: "camera"
        }
    }

    /// Date-kind icons: cake, heart, people, star, calendar.
    // from static/app/icons.js:161-163
    static func dateKindSymbol(_ kind: String) -> String {
        switch kind {
        case "birthday": "birthday.cake"
        case "anniversary": "heart"
        case "meeting": "person.2"
        case "event": "calendar"
        default: "star"
        }
    }

    // MARK: Categories

    // from static/app/api.js:188-190
    static func categoryLabel(_ category: String) -> String {
        switch category {
        case "personal": "Personal"
        case "professional": "Professional"
        case "both": "Both"
        case "unknown": "Not sure"
        default: ""
        }
    }

    /// The person's category tag is not drawn for "" or "unknown".
    // from static/app/people.js:1356
    static func showsCategoryTag(_ category: String) -> Bool { !category.isEmpty && category != "unknown" }

    // from static/css/tokens.css:174-178, dark :274-278
    static func categoryColor(_ category: String) -> Color {
        switch category {
        case "personal": Palette.dynamic(0x2F8A5B, 0x4FB583)
        case "professional": Palette.dynamic(0x3B6FE0, 0x5B8CF0)
        case "both": Palette.dynamic(0x7A5BD2, 0xA184E8)
        case "unknown": Palette.dynamic(0x9B9DA1, 0x6E7078)
        default: Palette.dynamic(0xD7D9DD, 0x3D3E4A)
        }
    }

    // MARK: Counts and words

    /// "person" or "people" for a count.
    // from static/app/api.js:197
    static func noun(_ n: Int, _ one: String, _ many: String) -> String { n == 1 ? one : many }

    /// Thousands separators, as `toLocaleString()` does.
    static func number(_ n: Int) -> String { n.formatted(.number) }

    /// The People count line: "1,250 people", "12 of 1,250 people", "1 person".
    // from static/app/people.js:152-175
    static func peopleCountLine(shown: Int, total: Int) -> String {
        let unit = noun(shown, "person", "people")
        return shown < total ? "\(number(shown)) of \(number(total)) \(unit)" : "\(number(shown)) \(unit)"
    }

    /// Ask's cost: 4 decimals under a cent, else 2.
    // from static/app/ask.js:414-415
    static func cost(usd: Double) -> String {
        usd < 0.01 ? String(format: "$%.4f", usd) : String(format: "$%.2f", usd)
    }

    // MARK: Lists

    // from static/app/lists.js:16-22
    static func listStatusLabel(_ status: String) -> String {
        switch status {
        case "invited": "Invited"
        case "replied": "Replied"
        case "attending": "Going"
        case "declined": "Declined"
        default: "To invite"
        }
    }

    static let listStatuses = ["", "invited", "replied", "attending", "declined"]

    /// A list whose name starts with an emoji (or flag) and a space shows that emoji as its
    /// icon and the rest as its title; otherwise icon is nil (draw the neutral diamond).
    // from static/app/lists.js:46-57
    static func listIcon(_ name: String) -> (icon: String?, title: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first, first.isPictograph else { return (nil, trimmed) }
        let rest = trimmed.dropFirst()
        guard let next = rest.first, next.isWhitespace else { return (nil, trimmed) }
        let title = rest.trimmingCharacters(in: .whitespacesAndNewlines)
        return title.isEmpty ? (nil, trimmed) : (String(first), title)
    }

    /// "4 people · 2 invited · 1 replied · 1 going", each extra only when above zero.
    // from static/app/lists.js (contract §8.9)
    static func listSummary(members: Int, invited: Int, replied: Int, attending: Int) -> String {
        var parts = ["\(number(members)) \(noun(members, "person", "people"))"]
        if invited > 0 { parts.append("\(invited) invited") }
        if replied > 0 { parts.append("\(replied) replied") }
        if attending > 0 { parts.append("\(attending) going") }
        return parts.joined(separator: " · ")
    }
}

extension Character {
    /// A leading pictograph or flag, as the web page's `\p{Extended_Pictographic}` or
    /// two `\p{Regional_Indicator}` match it.
    var isPictograph: Bool {
        guard let first = unicodeScalars.first else { return false }
        if (0x1F1E6...0x1F1FF).contains(first.value) { return unicodeScalars.count >= 2 }
        // Swift exposes no Extended_Pictographic; isEmoji minus ASCII (digits, #, *) matches it
        // for every character a list name could start with.
        return first.properties.isEmoji && first.value > 0x7F
    }
}

extension NSColor {
    /// CSS hsl(): hue in degrees, saturation and lightness 0...1.
    static func hsl(_ h: Double, _ s: Double, _ l: Double) -> NSColor {
        let c = (1 - abs(2 * l - 1)) * s
        let hp = h.truncatingRemainder(dividingBy: 360) / 60
        let x = c * (1 - abs(hp.truncatingRemainder(dividingBy: 2) - 1))
        let (r, g, b): (Double, Double, Double) =
            switch hp {
            case ..<1: (c, x, 0)
            case ..<2: (x, c, 0)
            case ..<3: (0, c, x)
            case ..<4: (0, x, c)
            case ..<5: (x, 0, c)
            default: (c, 0, x)
            }
        let m = l - c / 2
        return NSColor(srgbRed: r + m, green: g + m, blue: b + m, alpha: 1)
    }
}
