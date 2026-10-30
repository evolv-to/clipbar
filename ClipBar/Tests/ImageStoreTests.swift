import Cocoa
import CryptoKit

@main
struct ImageStoreTests {
    static func main() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("clipbar-native-images-" + UUID().uuidString)
        let images = root.appendingPathComponent("images")
        try fm.createDirectory(at: images, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1, pixelsHigh: 1,
                                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                      isPlanar: false, colorSpaceName: .deviceRGB,
                                      bytesPerRow: 4, bitsPerPixel: 32)!
        bitmap.bitmapData![0] = 255
        bitmap.bitmapData![1] = 0
        bitmap.bitmapData![2] = 0
        bitmap.bitmapData![3] = 255
        let data = bitmap.representation(using: .png, properties: [:])!
        let hash = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        let file = images.appendingPathComponent(hash + ".png")
        let outside = root.appendingPathComponent("outside.png")
        try data.write(to: file)
        try data.write(to: outside)
        func read(_ path: String?, id: String = hash, dir: URL = images) -> Data? {
            ImageStore.read(id, path: path, directory: dir)
        }
        precondition(read(file.path) == data)
        precondition(NSImage(data: read(file.path)!) != nil)
        precondition(read(outside.path) == nil)
        precondition(read(nil) == nil)
        precondition(read(file.path, id: "../outside") == nil)
        precondition(read(file.path, id: hash.uppercased()) == nil)
        try fm.removeItem(at: file)
        try fm.createSymbolicLink(at: file, withDestinationURL: outside)
        precondition(read(file.path) == nil)
        try fm.removeItem(at: file)
        try fm.linkItem(at: outside, to: file)
        precondition(read(file.path) == nil)
        try fm.removeItem(at: file)
        try Data("changed".utf8).write(to: file)
        precondition(read(file.path) == nil)
        try fm.removeItem(at: file)
        try fm.removeItem(at: images)
        try fm.createSymbolicLink(at: images, withDestinationURL: root)
        precondition(read(file.path) == nil)
        let preserved = try Data(contentsOf: outside)
        precondition(preserved == data)
        print("Native image store: valid PNG rendering and unsafe-path rejection passed")
    }
}
