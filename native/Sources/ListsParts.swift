import AppKit
import SwiftUI

// Small pieces the Lists screen draws with: the member avatar, the invite-status dot,
// the list icon, and the event-date check.

// MARK: - Event dates

/// The engine refuses a date that is not written YYYY-MM-DD with a 400 (NC-338; it used to
/// store "soon" as a list's event date, which the calendar then ignored). The screens still
/// check first, so the person is told in the field, in words, instead of getting a refusal.
enum ListsDates {
    /// The years an event is believed for (the Event date popover and its picker).
    static let eventYears = 1900...2200

    /// True for a real calendar day written as YYYY-MM-DD ("2026-02-30" is false), in a year
    /// within `years`.
    static func isValidDay(_ s: String, years: ClosedRange<Int> = ListsDates.eventYears) -> Bool {
        components(s, years: years) != nil
    }

    private static func components(_ s: String, years: ClosedRange<Int> = ListsDates.eventYears) -> DateComponents? {
        let parts = s.split(separator: "-", omittingEmptySubsequences: false)
        guard s.count == 10, parts.count == 3, parts[0].count == 4, parts[1].count == 2, parts[2].count == 2,
            parts.allSatisfy({ $0.allSatisfy(\.isASCII) && $0.allSatisfy(\.isNumber) }),
            let y = Int(parts[0]), let m = Int(parts[1]), let d = Int(parts[2]), years.contains(y)
        else { return nil }
        var c = DateComponents()
        c.year = y; c.month = m; c.day = d; c.hour = 12
        let cal = Calendar(identifier: .gregorian)
        guard let date = cal.date(from: c), cal.component(.day, from: date) == d, cal.component(.month, from: date) == m
        else { return nil }
        return c
    }

    /// The day as a local Date at noon, for the calendar picker.
    static func date(_ s: String) -> Date? {
        guard let c = components(s) else { return nil }
        return Calendar.current.date(from: c)
    }

    /// The picker's day back as YYYY-MM-DD, read in the local calendar (what was clicked).
    static func string(_ date: Date) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }
}

// MARK: - Invite status

enum ListsStatus {
    /// The statuses in pipeline order with the web page's labels (Display.listStatusLabel).
    static let all: [(value: String, label: String)] = Display.listStatuses.map { ($0, Display.listStatusLabel($0)) }

    /// A quiet dot beside the label, so a long roster can be scanned for who is going.
    static func color(_ status: String) -> Color {
        switch status {
        case "invited": Palette.accent
        case "replied": Palette.gold
        case "attending": Palette.green
        case "declined": Palette.red
        default: Palette.faint
        }
    }
}

struct ListsStatusLabel: View {
    let status: String
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(ListsStatus.color(status)).frame(width: 7, height: 7)
            Text(Display.listStatusLabel(status))
                .foregroundStyle(status.isEmpty ? Palette.muted : Palette.ink)
        }
    }
}

// MARK: - List icon

/// A leading emoji the person typed into the name, or the neutral diamond.
struct ListsIcon: View {
    let name: String
    var size: CGFloat = 15

    var body: some View {
        let icon = Display.listIcon(name).icon
        Group {
            if let icon {
                Text(verbatim: icon).font(.system(size: size))
            } else {
                Image(systemName: "diamond")
                    .font(.system(size: size * 0.8, weight: .regular))
                    .foregroundStyle(Palette.faint)
            }
        }
        .frame(width: size * 1.5)
        .accessibilityHidden(true)
    }
}

// MARK: - Avatar

/// Photos already fetched this session, keyed by the engine's photo path (which carries
/// the file's modification time, so a new photo is a new key).
@MainActor
enum ListsPhotoCache {
    static var images: [String: NSImage] = [:]
    static var missing: Set<String> = []
}

/// A member's photo or monogram, with the closeness ring (contract §8.4).
struct ListsAvatar: View {
    let person: PersonSummary
    var size: CGFloat = 24
    var ring = true
    @AppEnvironment private var app
    @State private var image: NSImage?

    var body: some View {
        let tier = Display.Tier(label: person.strengthLabel)
        let line: CGFloat = size >= 28 ? 2.5 : 2
        let inner = ring ? size - line * 2 - 2 : size
        ZStack {
            if ring {
                Circle().inset(by: line / 2).stroke(Palette.line, lineWidth: line)
                Circle().inset(by: line / 2)
                    .trim(from: 0, to: Display.ringFraction(strength: person.strength))
                    .stroke(tier.color, style: StrokeStyle(lineWidth: line, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            face
                .frame(width: inner, height: inner)
                .clipShape(Circle())
        }
        .frame(width: size, height: size)
        .help(Display.ringTooltip(strength: person.strength, label: person.strengthLabel))
        .task(id: person.photo) { await loadPhoto() }
    }

    @ViewBuilder private var face: some View {
        if let image = image ?? ListsPhotoCache.images[person.photo] {
            Image(nsImage: image).resizable().scaledToFill()
        } else {
            let c = Display.avatarColors(person.name)
            ZStack {
                LinearGradient(colors: [c.top, c.bottom], startPoint: .topLeading, endPoint: .bottomTrailing)
                Text(verbatim: Display.initials(person.name))
                    .font(.system(size: max(8, size * 0.38), weight: .semibold))
                    .foregroundStyle(c.ink)
                    .minimumScaleFactor(0.6)
            }
        }
    }

    private func loadPhoto() async {
        let key = person.photo
        guard !key.isEmpty, ListsPhotoCache.images[key] == nil, !ListsPhotoCache.missing.contains(key) else { return }
        do {
            if let data = try await app.client.photoData(key), let img = NSImage(data: data) {
                ListsPhotoCache.images[key] = img
                image = img
            } else {
                ListsPhotoCache.missing.insert(key)
            }
        } catch {
            // A failed photo just leaves the initials, as the web page does.
        }
    }
}

// MARK: - Dragging a member onto another list

/// What a member row carries when dragged: plain text naming the person, so a drop
/// anywhere else (a note, a message) reads as nothing worse than a tag.
enum ListsDrag {
    static let prefix = "network-crm-person:"
    static func payload(_ id: Int) -> String { prefix + String(id) }
    static func personID(_ s: String) -> Int? {
        s.hasPrefix(prefix) ? Int(s.dropFirst(prefix.count)) : nil
    }
}
