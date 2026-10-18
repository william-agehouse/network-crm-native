import SwiftUI

// Acting on many people at once: the "Add these 42 to a list" control under a narrowed
// table (people.js:903-907), and the bar that replaces it while several rows are selected
// with ⌘-click or ⇧-click (people.js:909-917). Both add through the same menu, which lists
// every list plus "New List…", and say what the engine actually added (ui.js:373-421).

/// "Add to a list" for a set of people: existing lists, then a new one.
struct PeopleAddToListMenu<Label: View>: View {
    let ids: [Int]
    let extras: PeopleExtrasStore
    /// Adds to an existing list; the screen says how it went.
    let onAdd: (CRMList) -> Void
    /// Asks for a name, then creates the list with these people in it.
    let onNewList: () -> Void
    @ViewBuilder var label: Label

    var body: some View {
        Menu {
            // ui.js:402
            Text(verbatim: "Add \(Display.number(ids.count)) \(Display.noun(ids.count, "person", "people")) to a list")
            Divider()
            if !extras.listsLoaded {
                Text("Loading lists…")
            } else if extras.lists.isEmpty {
                Text("No lists yet")
            }
            ForEach(extras.lists) { list in
                Button {
                    onAdd(list)
                } label: {
                    Text(verbatim: PersonListRow.title(list))
                    Text(verbatim: "\(Display.number(list.members)) \(Display.noun(list.members, "person", "people"))")
                }
            }
            Divider()
            Button("New List…", action: onNewList)
        } label: {
            label
        }
        .disabled(ids.isEmpty)
    }
}

/// What the status bar offers for many people: the selection, or the narrowed list.
struct PeopleBulkBar: View {
    let model: PeopleModel
    let extras: PeopleExtrasStore
    /// Rows selected with ⌘ or ⇧ (more than one), in table order.
    let selected: [Int]
    let onAdd: ([Int], CRMList) -> Void
    let onNewList: ([Int]) -> Void
    let onClear: () -> Void

    var body: some View {
        if selected.count > 1 {
            HStack(spacing: 8) {
                Text(verbatim: "\(Display.number(selected.count)) selected")
                    .fontWeight(.semibold)
                    .foregroundStyle(Palette.ink)
                PeopleAddToListMenu(
                    ids: selected, extras: extras, onAdd: { onAdd(selected, $0) }, onNewList: { onNewList(selected) }
                ) {
                    Text("Add to List")
                }
                .fixedSize()
                Button("Clear", action: onClear)
                    .help("Clear the selection (Esc)")
            }
            .controlSize(.small)
        } else if model.isNarrowed && !model.visible.isEmpty {
            let ids = model.visible.map(\.id)
            // people.js:172
            let title = ids.count == 1 ? "Add This Person to a List" : "Add These \(Display.number(ids.count)) to a List"
            PeopleAddToListMenu(ids: ids, extras: extras, onAdd: { onAdd(ids, $0) }, onNewList: { onNewList(ids) }) {
                Text(verbatim: title)
            }
            .controlSize(.small)
            .fixedSize()
        }
    }
}
