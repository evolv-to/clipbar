import Foundation
import Darwin

@main
struct PeerStatusTests {
    static func main() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("clipbar-native-status-" + UUID().uuidString)
        let config = root.appendingPathComponent("config")
        try fm.createDirectory(at: config, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }
        let file = config.appendingPathComponent("peer-status.json")
        let data = Data("{\"lan\":[{\"hostname\":\"synthetic-peer\"}]}".utf8)
        try data.write(to: file)
        chmod(file.path, 0o600)
        precondition(PeerStatus.read(directory: config) == data)
        precondition(PeerStatus.read(directory: config, now: Date().addingTimeInterval(20)) == nil)
        chmod(file.path, 0o644)
        precondition(PeerStatus.read(directory: config) == nil)
        chmod(file.path, 0o600)
        let outside = root.appendingPathComponent("outside.json")
        try fm.moveItem(at: file, to: outside)
        try fm.createSymbolicLink(at: file, withDestinationURL: outside)
        precondition(PeerStatus.read(directory: config) == nil)
        try fm.removeItem(at: file)
        try fm.linkItem(at: outside, to: file)
        precondition(PeerStatus.read(directory: config) == nil)
        try fm.removeItem(at: file)
        try fm.removeItem(at: config)
        try fm.createSymbolicLink(at: config, withDestinationURL: root)
        precondition(PeerStatus.read(directory: config) == nil)
        print("Native peer status: owner-only, fresh snapshots accepted; unsafe state rejected")
    }
}
