import Cocoa
import Darwin

@main
struct PrivateStateTests {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("clipbar-native-private-" + UUID().uuidString)
        setenv("CLIPBAR_CONFIG_DIR", root.path, 1)
        defer { try? FileManager.default.removeItem(at: root) }
        guard let shared = PrivateState.lock() else { fatalError("profile lock failed") }
        let second = open(PrivateState.path("runtime.lock"), O_RDWR)
        precondition(second >= 0)
        precondition(flock(second, LOCK_EX | LOCK_NB) != 0)
        close(shared)
        precondition(flock(second, LOCK_EX | LOCK_NB) == 0)
        close(second)
        let board = NSPasteboard(name: NSPasteboard.Name("clipbar.synthetic." + UUID().uuidString))
        defer { board.releaseGlobally() }
        for marker in PrivateState.excludedTypes {
            board.clearContents()
            board.setString("synthetic confidential content", forType: .string)
            board.setData(Data(), forType: NSPasteboard.PasteboardType(marker))
            precondition(PrivateState.excluded(board))
        }
        board.clearContents()
        board.setString("synthetic ordinary content", forType: .string)
        precondition(!PrivateState.excluded(board))
        let target = root.appendingPathComponent("outside")
        try Data("synthetic sentinel".utf8).write(to: target)
        try FileManager.default.createSymbolicLink(atPath: PrivateState.path(".paused"), withDestinationPath: target.path)
        PrivateState.setFlag(".paused", enabled: true)
        let sentinel = try Data(contentsOf: target)
        precondition(sentinel == Data("synthetic sentinel".utf8))
        let attrs = try FileManager.default.attributesOfItem(atPath: PrivateState.path(".paused"))
        precondition((attrs[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        print("Native privacy: sensitive markers, private flags and maintenance exclusion passed")
    }
}
