import Cocoa
import CryptoKit
import Darwin

enum ImageStore {
    static let maxBytes = 16 * 1024 * 1024
    static let directory = URL(fileURLWithPath: NSHomeDirectory() + "/.config/clipd/images")

    static func read(_ identifier: String, path: String?, directory: URL = directory) -> Data? {
        let digits = Array(identifier.utf8)
        guard digits.count == 64,
              digits.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else { return nil }
        let name = identifier + ".png"
        guard path == directory.appendingPathComponent(name).path else { return nil }
        let parent = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard parent >= 0 else { return nil }
        defer { close(parent) }
        var parentInfo = stat()
        guard fstat(parent, &parentInfo) == 0, parentInfo.st_uid == getuid() else { return nil }
        let fd = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? file.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_nlink == 1, info.st_uid == getuid(), info.st_size <= maxBytes,
              let data = try? file.read(upToCount: maxBytes + 1),
              data.count >= 33, data.count <= maxBytes else { return nil }
        guard SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() == identifier else { return nil }
        let header = Array(data.prefix(24))
        guard Array(header.prefix(16)) == [137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82] else { return nil }
        let width = header[16..<20].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
        let height = header[20..<24].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
        guard width > 0, height > 0, width * height <= 20_000_000,
              let image = NSBitmapImageRep(data: data), image.pixelsWide == width,
              image.pixelsHigh == height else { return nil }
        return data
    }
}
