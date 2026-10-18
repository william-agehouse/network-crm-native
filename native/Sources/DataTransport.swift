import Foundation

// Two engine calls the shared EngineClient cannot make, kept beside it in an extension so
// EngineClient.swift (a shared file) is untouched. Both follow its rules (contract §1):
// the private connection (or http://127.0.0.1:PORT in development), no Origin, no
// Sec-Fetch-Site, a Content-Length body, JSON error
// bodies read only when they say they are JSON, and one retry when an earlier rejected
// write left the kept-alive connection poisoned (an HTML 400/501).
//
//   rawGet    a GET whose answer is a file, not JSON (the backup, GET /api/export)
//   longPost  a POST that may legitimately run for up to an hour: one import
//             (POST /api/import/run). The shared session stops every request after
//             EngineClient.importTimeout + 60 s (375 s), but the engine's own ceiling on
//             one import is NC_IMPORT_TIMEOUT_SEC, up to 3,600 s (crm/api_import.py:58-75).
//             A page that gives up first reports an import that is still running, and
//             then succeeds, as a failure (NC-176, static/app/api.js:58-88).

extension EngineClient {
    /// The longest one import may take on the engine (what import_timeout_sec() clamps to),
    /// plus room for its answer to arrive.
    static let longestImport: TimeInterval = 3600 + 120

    /// Its own ephemeral session, set up like the shared one (no cache, no cookies), with a
    /// resource limit long enough for the longest import.
    private static let longSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.timeoutIntervalForRequest = defaultTimeout
        config.timeoutIntervalForResource = longestImport
        config.httpAdditionalHeaders = ["Accept": "application/json"]
        config.httpMaximumConnectionsPerHost = 4
        return URLSession(configuration: config)
    }()

    private struct RawErrorBody: Decodable { let error: String? }

    /// One request on the long session. Returns the bytes of a 2xx answer; throws the
    /// engine's own sentence otherwise.
    private func rawSend(_ method: String, _ path: String, query: [String: String], body: Data?, timeout: TimeInterval)
        async throws -> (Data, HTTPURLResponse)
    {
        var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        c.path = path
        if !query.isEmpty {
            c.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
            // The engine's parse_qs reads "+" as a space.
            c.percentEncodedQuery = c.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        }
        var req = URLRequest(url: c.url!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.httpMethod = method
        if method == "POST" {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = body ?? Data("{}".utf8)
        }
        var attempt = 0
        while true {
            attempt += 1
            let data: Data
            let resp: URLResponse
            do {
                (data, resp) = try await exchange(req, on: Self.longSession)
            } catch let e as URLError {
                switch e.code {
                case .cancelled: throw CancellationError()
                case .timedOut: throw logged(EngineError.timedOut(seconds: Int(timeout)), path)
                case .networkConnectionLost where attempt == 1 && method == "GET": continue
                default: throw logged(EngineError.unreachable(port: port), path)
                }
            }
            guard let http = resp as? HTTPURLResponse else { throw logged(EngineError.unexpectedAnswer(status: 0), path) }
            let type = (http.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased()
            if (200..<300).contains(http.statusCode) { return (data, http) }
            // A poisoned connection answers before the engine read this request at all,
            // so sending it again cannot run anything twice.
            if attempt == 1, http.statusCode == 400 || http.statusCode == 501, !type.contains("application/json") {
                continue
            }
            if type.contains("application/json"), let b = try? JSONDecoder().decode(RawErrorBody.self, from: data),
                let message = b.error
            {
                throw logged(EngineError.engine(status: http.statusCode, message: message), path)
            }
            throw logged(EngineError.unexpectedAnswer(status: http.statusCode), path)
        }
    }

    /// The detail a person is not shown goes to the log, as EngineClient's own calls do
    /// (NC-363), so a long import that failed leaves a trace.
    private func logged(_ e: EngineError, _ route: String) -> EngineError {
        var message = ""
        if case .engine(_, let m) = e { message = m }
        Self.log.error("\(route, privacy: .public): \(e.technicalDetail, privacy: .public) \(message, privacy: .private)")
        return e
    }

    /// A GET whose answer is a file: the bytes and the engine's own file name (from
    /// Content-Disposition).
    func rawGet(_ path: String, query: [String: String] = [:], timeout: TimeInterval = 120)
        async throws -> (data: Data, fileName: String?)
    {
        let (data, http) = try await rawSend("GET", path, query: query, body: nil, timeout: timeout)
        return (data, Self.fileName(http.value(forHTTPHeaderField: "Content-Disposition")))
    }

    /// A POST that may run for a long time, decoded like every other answer.
    func longPost<T: Decodable>(_ path: String, body: [String: JSONValue], as type: T.Type = T.self, timeout: TimeInterval)
        async throws -> T
    {
        let limit = min(max(timeout, Self.defaultTimeout), Self.longestImport)
        let (data, _) = try await rawSend("POST", path, query: [:], body: try JSONEncoder().encode(body), timeout: limit)
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch let e as DecodingError {
            let (where_, problem) = Self.describe(e)
            throw logged(EngineError.decoding(route: path, path: where_, problem: problem), path)
        }
    }

    /// `attachment; filename="network-crm-export-2026-10-16.csv"` → the name. Only a plain
    /// file name, never a path.
    static func fileName(_ disposition: String?) -> String? {
        guard let d = disposition, let r = d.range(of: #"filename="([^"]+)""#, options: .regularExpression) else { return nil }
        let name = String(d[r]).replacingOccurrences(of: "filename=", with: "")
            .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        return name.contains("/") || name.isEmpty ? nil : name
    }
}
