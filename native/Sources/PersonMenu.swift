import SwiftUI

// The person page's "More actions" menu (⋯), as data: which items a person gets depends on
// whether they are hidden or deleted, so the menu and its --snapshot picture are both drawn
// from this one list. Groups, order and notes from static/app/people.js:1453-1505 (HeadMenu).

struct PersonMenuItem: Identifiable {
    enum Action: Equatable {
        case merge
        case category(String)
        case setPhoto, removePhoto
        case restore, purge, exclude, unexclude, delete
    }

    let title: String
    /// The quiet second line (the web page's r-meta).
    var note: String? = nil
    let action: Action
    var destructive = false
    var id: String { title }

    struct MenuGroup {
        let title: String
        let items: [PersonMenuItem]
    }

    static func sections(for p: PersonDetail) -> [MenuGroup] {
        var danger: [PersonMenuItem]
        if p.deleted {
            danger = [
                .init(title: "Restore", note: "bring this deleted contact back", action: .restore),
                // The web page's second click ("Yes — erase permanently") is the confirmation
                // dialog here.
                .init(title: "Delete Permanently…", note: "cannot be undone", action: .purge, destructive: true),
            ]
        } else {
            danger = [
                p.excluded
                    ? .init(title: "Unexclude…", action: .unexclude)
                    : .init(title: "Exclude…", note: "hide everywhere", action: .exclude, destructive: true),
                .init(title: "Delete…", note: "reversible — restore from the Deleted filter", action: .delete, destructive: true),
            ]
        }
        var photo = [PersonMenuItem(title: "Set Photo…", action: .setPhoto)]
        // The web page offers "Remove photo" to everyone; with no photo it would do nothing.
        if !p.photo.isEmpty { photo.append(.init(title: "Remove Photo", action: .removePhoto)) }
        return [
            MenuGroup(title: "Duplicates", items: [
                .init(title: "Merge with…", note: "fold another record into this one", action: .merge)
            ]),
            MenuGroup(title: "Category", items: ["personal", "professional", "both", "unknown"].map {
                .init(title: Display.categoryLabel($0), action: .category($0))
            }),
            MenuGroup(title: "Photo", items: photo),
            MenuGroup(title: "Danger", items: danger),
        ]
    }
}

/// The menu's items drawn as a still picture, for `--snapshot` only (a menu is a window of
/// its own, which a picture of the main window never shows).
struct PersonMenuPicture: View {
    let p: PersonDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(PersonMenuItem.sections(for: p), id: \.title) { section in
                Text(section.title)
                    .font(.system(size: Metrics.fsOverline, weight: .semibold))
                    .foregroundStyle(Palette.muted)
                    .padding(.top, 6)
                ForEach(section.items) { item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Image(systemName: "checkmark")
                            .font(.system(size: 10, weight: .semibold))
                            .opacity(item.action == .category(p.category) ? 1 : 0)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(item.title)
                                .foregroundStyle(item.destructive ? Palette.danger : Palette.ink)
                            if let note = item.note {
                                Text(note).font(.system(size: Metrics.fsOverline)).foregroundStyle(Palette.muted)
                            }
                        }
                    }
                    .font(.system(size: Metrics.fsUI))
                    .padding(.vertical, 2)
                }
            }
        }
        .padding(12)
        .frame(width: 300, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel2))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall).strokeBorder(Palette.line))
    }
}
