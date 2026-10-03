import Metal
import Foundation

let args = CommandLine.arguments
let seconds = args.count > 1 ? (Double(args[1]) ?? 60.0) : 60.0

guard let device = MTLCreateSystemDefaultDevice() else {
    print("FAIL: Metal qurilma yo'q")
    exit(1)
}
print("GPU: \(device.name)")

let src = """
#include <metal_stdlib>
using namespace metal;
kernel void burn(device float *out [[buffer(0)]],
                 constant uint &seed [[buffer(1)]],
                 constant uint &iters [[buffer(2)]],
                 uint gid [[thread_position_in_grid]]) {
  float x = float((gid + seed) % 4096u) * 0.001f + 1.0f;
  float y = 1.000001f;
  float c = 0.000001f;
  for (uint k = 0; k < iters; k++) {
    x = fma(x, y, c);
    y = fma(y, 0.999999f, c);
  }
  out[gid % 4096u] = x + y;
}
"""

let lib = try device.makeLibrary(source: src, options: nil)
let fn = lib.makeFunction(name: "burn")!
let pipe = try device.makeComputePipelineState(function: fn)
let queue = device.makeCommandQueue()!
let out = device.makeBuffer(length: 4096 * 4, options: .storageModeShared)!
var iters: UInt32 = 2_000_000
let threads = pipe.maxTotalThreadsPerThreadgroup
let grid = MTLSize(width: 65536, height: 1, depth: 1)
let tg = MTLSize(width: threads, height: 1, depth: 1)

let t0 = Date()
var n = 0
var seed: UInt32 = 1
while Date().timeIntervalSince(t0) < seconds {
    let cmd = queue.makeCommandBuffer()!
    let enc = cmd.makeComputeCommandEncoder()!
    enc.setComputePipelineState(pipe)
    enc.setBuffer(out, offset: 0, index: 0)
    enc.setBytes(&seed, length: 4, index: 1)
    enc.setBytes(&iters, length: 4, index: 2)
    enc.dispatchThreads(grid, threadsPerThreadgroup: tg)
    enc.endEncoding()
    cmd.commit()
    cmd.waitUntilCompleted()
    n += 1
    seed &+= 1
    if n % 20 == 0 {
        print("  ... \(Int(Date().timeIntervalSince(t0)))s launch=\(n)")
        fflush(stdout)
    }
}

print("METAL PASS: \(Int(seconds))s launch=\(n) gpu=\(device.name)")
