import Foundation
import Darwin

enum PeerStatus {
    static func read(directory: URL = URL(fileURLWithPath: NSHomeDirectory() + "/.config/clipd"),
                     now: Date = Date()) -> Data? {
        let parent = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard parent >= 0 else { return nil }
        defer { close(parent) }
        var parentInfo = stat()
        guard fstat(parent, &parentInfo) == 0, parentInfo.st_uid == getuid() else { return nil }
        let fd = openat(parent, "peer-status.json", O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? file.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), info.st_nlink == 1, (info.st_mode & 0o777) == 0o600,
              info.st_size <= 256 * 1024 else { return nil }
        let age = now.timeIntervalSince1970 - Double(info.st_mtimespec.tv_sec)
        guard age >= -1, age <= 10 else { return nil }
        guard let data = try? file.read(upToCount: 256 * 1024 + 1), data.count <= 256 * 1024 else { return nil }
        return data
    }
}
