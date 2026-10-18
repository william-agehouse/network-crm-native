import Foundation

// NC-330. How the app reaches its engine when nothing else on the Mac may.
//
// The engine the app starts listens on a socket FILE (crm/app.py --socket), not on a
// network port: no browser, page or other program can open an address to it, and the file
// and its folder open for this account only. URLSession cannot speak to a socket file, so
// this sends the same HTTP/1.1 requests over it by hand: one connection per request,
// "Connection: close", the answer read until the engine hangs up.
//
// Failures come back as the URLError codes URLSession would raise for the same thing, so
// EngineClient's one retry-and-explain path serves both connections.

/// Where the engine is reached.
enum EngineAddress: Equatable, Sendable {
    /// Development only (NC_NATIVE_PORT): an engine someone started on 127.0.0.1.
    case port(Int)
    /// The app: the private socket file of the engine it started.
    case socket(String)

    var port: Int {
        if case .port(let p) = self { return p }
        return 0
    }
}

enum PrivateConnection {
    /// The longest path a socket file can have on macOS (sockaddr_un.sun_path, less its 0).
    static let longestPath = 103
    /// Answers bigger than this are refused rather than held in memory: the largest real
    /// one (a full export) is a few megabytes.
    static let largestAnswer = 512 * 1024 * 1024

    /// One request over the socket file at `path`. The URL's host and port are ignored;
    /// its path and query are sent. Honours `req.timeoutInterval` and task cancellation.
    static func data(for req: URLRequest, socketPath path: String) async throws -> (Data, HTTPURLResponse) {
        let wire = try request(req)
        let deadline = Date().addingTimeInterval(req.timeoutInterval > 0 ? req.timeoutInterval : 60)
        let box = SocketBox()
        return try await withTaskCancellationHandler {
            try await Task.detached(priority: .userInitiated) {
                try exchange(wire, path: path, deadline: deadline, box: box, url: req.url)
            }.value
        } onCancel: {
            box.cancel()
        }
    }

    /// Whether an engine answers on `path` at all: a connect, nothing sent.
    static func answers(_ path: String) -> Bool {
        guard let fd = try? connect(path) else { return false }
        close(fd)
        return true
    }

    // MARK: The request

    static func request(_ req: URLRequest) throws -> Data {
        guard let url = req.url else { throw URLError(.badURL) }
        let method = req.httpMethod ?? "GET"
        var target = url.path(percentEncoded: true)
        if target.isEmpty { target = "/" }
        if let q = url.query(percentEncoded: true), !q.isEmpty { target += "?" + q }
        let body = req.httpBody ?? Data()
        var head = "\(method) \(target) HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
        var fields = req.allHTTPHeaderFields ?? [:]
        if fields["Accept"] == nil { fields["Accept"] = "application/json" }
        for (name, value) in fields.sorted(by: { $0.key < $1.key }) {
            let lower = name.lowercased()
            if lower == "host" || lower == "connection" || lower == "content-length" { continue }
            // A header can never carry a line break of its own into the request.
            guard !name.contains(where: \.isNewline), !value.contains(where: \.isNewline) else {
                throw URLError(.badURL)
            }
            head += "\(name): \(value)\r\n"
        }
        if !body.isEmpty || method == "POST" || method == "PUT" {
            head += "Content-Length: \(body.count)\r\n"
        }
        head += "\r\n"
        return Data(head.utf8) + body
    }

    // MARK: The answer

    struct Parsed {
        let status: Int
        let headers: [String: String]
        let body: Data
    }

    /// Splits a whole HTTP/1.x answer into its status, headers and body. Handles a body
    /// sized by Content-Length, sent in chunks, or running to the end of the connection.
    static func parse(_ raw: Data) throws -> Parsed {
        let gap = Data("\r\n\r\n".utf8)
        guard let split = raw.range(of: gap) else { throw URLError(.cannotParseResponse) }
        guard let head = String(data: raw[raw.startIndex..<split.lowerBound], encoding: .isoLatin1) else {
            throw URLError(.cannotParseResponse)
        }
        var lines = head.components(separatedBy: "\r\n")
        let statusLine = lines.removeFirst()
        let parts = statusLine.split(separator: " ", maxSplits: 2)
        guard parts.count >= 2, parts[0].hasPrefix("HTTP/1."), let status = Int(parts[1]) else {
            throw URLError(.cannotParseResponse)
        }
        var headers: [String: String] = [:]
        for line in lines {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = String(line[..<colon]).trimmingCharacters(in: .whitespaces)
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            if let earlier = headers[name] { headers[name] = earlier + ", " + value } else { headers[name] = value }
        }
        var body = Data(raw[split.upperBound...])
        let lowered = Dictionary(headers.map { ($0.key.lowercased(), $0.value) }, uniquingKeysWith: { a, _ in a })
        if lowered["transfer-encoding"]?.lowercased().contains("chunked") == true {
            body = try unchunk(body)
        } else if let raw = lowered["content-length"], let n = Int(raw), n >= 0 {
            guard body.count >= n else { throw URLError(.networkConnectionLost) }
            body = body.prefix(n)
        }
        return Parsed(status: status, headers: headers, body: body)
    }

    static func unchunk(_ data: Data) throws -> Data {
        var out = Data()
        var i = data.startIndex
        let crlf = Data("\r\n".utf8)
        while true {
            guard let end = data.range(of: crlf, in: i..<data.endIndex) else { throw URLError(.networkConnectionLost) }
            let sizeText = String(data: data[i..<end.lowerBound], encoding: .ascii)?
                .split(separator: ";").first.map(String.init)?.trimmingCharacters(in: .whitespaces) ?? ""
            guard let size = Int(sizeText, radix: 16), size >= 0 else { throw URLError(.cannotParseResponse) }
            i = end.upperBound
            if size == 0 { return out }
            guard data.distance(from: i, to: data.endIndex) >= size + 2 else { throw URLError(.networkConnectionLost) }
            let next = data.index(i, offsetBy: size)
            out.append(data[i..<next])
            i = data.index(next, offsetBy: 2)
        }
    }

    // MARK: The socket

    /// The open connection, so a cancel from another thread can end a read that waits.
    final class SocketBox: @unchecked Sendable {
        private let lock = NSLock()
        private var fd: Int32 = -1
        private var cancelled = false

        func set(_ value: Int32) -> Bool {
            lock.lock(); defer { lock.unlock() }
            if cancelled { return false }
            fd = value
            return true
        }

        var isCancelled: Bool {
            lock.lock(); defer { lock.unlock() }
            return cancelled
        }

        func cancel() {
            lock.lock(); defer { lock.unlock() }
            cancelled = true
            if fd >= 0 { shutdown(fd, SHUT_RDWR) }
        }

        func clear() {
            lock.lock(); defer { lock.unlock() }
            fd = -1
        }
    }

    static func connect(_ path: String) throws -> Int32 {
        let bytes = Array(path.utf8)
        guard !bytes.isEmpty, bytes.count <= longestPath else { throw URLError(.badURL) }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw URLError(.cannotConnectToHost) }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard result == 0 else {
            close(fd)
            throw URLError(.cannotConnectToHost)
        }
        return fd
    }

    /// Waits until `fd` is ready for `events`, or throws at the deadline or on cancel.
    private static func wait(_ fd: Int32, _ events: Int16, deadline: Date, box: SocketBox) throws {
        while true {
            if box.isCancelled { throw URLError(.cancelled) }
            let left = deadline.timeIntervalSinceNow
            if left <= 0 { throw URLError(.timedOut) }
            var p = pollfd(fd: fd, events: events, revents: 0)
            // Wake at least every half second to notice a cancel.
            let n = poll(&p, 1, Int32(min(left, 0.5) * 1000) + 1)
            if n > 0 { return }
            if n < 0 && errno != EINTR { throw URLError(.networkConnectionLost) }
        }
    }

    private static func exchange(
        _ wire: Data, path: String, deadline: Date, box: SocketBox, url: URL?
    ) throws -> (Data, HTTPURLResponse) {
        let fd = try connect(path)
        defer {
            box.clear()
            close(fd)
        }
        guard box.set(fd) else { throw URLError(.cancelled) }
        let flags = fcntl(fd, F_GETFL)
        _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)

        var sent = 0
        try wire.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            while sent < raw.count {
                try wait(fd, Int16(POLLOUT), deadline: deadline, box: box)
                let n = write(fd, raw.baseAddress!.advanced(by: sent), raw.count - sent)
                if n > 0 { sent += n; continue }
                if n < 0 && (errno == EAGAIN || errno == EINTR) { continue }
                throw URLError(box.isCancelled ? .cancelled : .networkConnectionLost)
            }
        }

        var answer = Data()
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            try wait(fd, Int16(POLLIN), deadline: deadline, box: box)
            let n = read(fd, &buffer, buffer.count)
            if n > 0 {
                answer.append(buffer, count: n)
                if answer.count > largestAnswer { throw URLError(.dataLengthExceedsMaximum) }
                continue
            }
            if n == 0 { break }
            if errno == EAGAIN || errno == EINTR { continue }
            throw URLError(box.isCancelled ? .cancelled : .networkConnectionLost)
        }
        if box.isCancelled { throw URLError(.cancelled) }
        guard !answer.isEmpty else { throw URLError(.networkConnectionLost) }
        let parsed = try parse(answer)
        let where_ = url ?? URL(string: "http://localhost/")!
        guard let http = HTTPURLResponse(url: where_, statusCode: parsed.status, httpVersion: "HTTP/1.1",
                                         headerFields: parsed.headers) else {
            throw URLError(.cannotParseResponse)
        }
        return (parsed.body, http)
    }
}
