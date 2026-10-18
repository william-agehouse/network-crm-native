import AppKit
import SwiftUI

// The People table's round avatar: the person's photo when they have one, otherwise their
// initials on the web page's hashed two-colour gradient (Display.avatarColors). Photos are
// fetched only when a row is actually drawn (a Table makes cells for the rows on screen),
// and kept in memory, so scrolling back never fetches twice and thousands of photos are
// never asked for at once.

@MainActor
final class PeoplePhotoCache {
    static let shared = PeoplePhotoCache()

    private let images = NSCache<NSString, NSImage>()
    /// Photo paths that answered 404 or would not decode: fall back to initials, don't retry.
    private var missing = Set<String>()
    /// One request per photo, however many cells ask for it at once.
    private var inFlight: [String: Task<NSImage?, Never>] = [:]

    private init() { images.countLimit = 600 }

    /// Keyed on the full relative path, which carries the photo's `v` (its mtime), so a
    /// replaced photo is a new key (contract §4, trap 4).
    func cached(_ path: String) -> NSImage? { path.isEmpty ? nil : images.object(forKey: path as NSString) }

    func isMissing(_ path: String) -> Bool { missing.contains(path) }

    func image(for path: String, client: EngineClient) async -> NSImage? {
        guard !path.isEmpty, !missing.contains(path) else { return nil }
        if let hit = images.object(forKey: path as NSString) { return hit }
        if let running = inFlight[path] { return await running.value }
        let task = Task<NSImage?, Never> {
            guard let data = try? await client.photoData(path) else { return nil }
            return NSImage(data: data)
        }
        inFlight[path] = task
        let image = await task.value
        inFlight[path] = nil
        if let image {
            images.setObject(image, forKey: path as NSString)
        } else if !Task.isCancelled {
            missing.insert(path)
        }
        return image
    }
}

struct PeopleAvatar: View {
    let name: String
    let photo: String
    var size: CGFloat = 26
    /// Passed in rather than read from the environment: Table cells are hosted by AppKit,
    /// and an @Observable environment object does not always reach them.
    let client: EngineClient

    @State private var image: NSImage?

    init(name: String, photo: String, size: CGFloat = 26, client: EngineClient) {
        self.name = name
        self.photo = photo
        self.size = size
        self.client = client
        _image = State(initialValue: PeoplePhotoCache.shared.cached(photo))
    }

    var body: some View {
        ZStack {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fill)
            } else {
                monogram
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(Palette.line.opacity(image == nil ? 0 : 1), lineWidth: 0.5))
        .accessibilityHidden(true)
        .task(id: photo) {
            if let hit = PeoplePhotoCache.shared.cached(photo) {
                image = hit
                return
            }
            image = nil
            guard !photo.isEmpty else { return }
            let loaded = await PeoplePhotoCache.shared.image(for: photo, client: client)
            if !Task.isCancelled { image = loaded }
        }
    }

    // from static/css/ui.css:430-441 — gradient at 135°, initials at 38% of the diameter, weight 650
    private var monogram: some View {
        let c = Display.avatarColors(name)
        return ZStack {
            LinearGradient(colors: [c.top, c.bottom], startPoint: .topLeading, endPoint: .bottomTrailing)
            Text(Display.initials(name))
                .font(.system(size: size * 0.38, weight: .semibold))
                .foregroundStyle(c.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
        }
    }
}
