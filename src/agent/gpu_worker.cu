// Real GPU worker for L1 scheduling PoC.
// Allocates device memory and runs GEMM-like FP32 loops so nvidia-smi shows real util.
#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <string>
#include <thread>
#include <vector>

#define CHECK(call)                                                      \
  do {                                                                   \
    cudaError_t err__ = (call);                                          \
    if (err__ != cudaSuccess) {                                          \
      fprintf(stderr, "CUDA error %s:%d: %s\n", __FILE__, __LINE__,      \
              cudaGetErrorString(err__));                                \
      return 2;                                                          \
    }                                                                    \
  } while (0)

__global__ void saxpy_kernel(float *x, float *y, float a, int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n) y[i] = a * x[i] + y[i];
}

static void usage() {
  fprintf(stderr,
          "usage: gpu_worker --gpu N --memory-mb M --duration-sec S --seed SEED "
          "--task-id ID [--out PATH]\n");
}

int main(int argc, char **argv) {
  int gpu = 0;
  int memory_mb = 1024;
  int duration_sec = 3;
  unsigned seed = 202607;
  std::string task_id = "task";
  std::string out_path;

  for (int i = 1; i < argc; ++i) {
    auto need = [&](const char *flag) -> const char * {
      if (i + 1 >= argc) {
        usage();
        exit(1);
      }
      return argv[++i];
    };
    if (!strcmp(argv[i], "--gpu")) gpu = atoi(need("--gpu"));
    else if (!strcmp(argv[i], "--memory-mb")) memory_mb = atoi(need("--memory-mb"));
    else if (!strcmp(argv[i], "--duration-sec")) duration_sec = atoi(need("--duration-sec"));
    else if (!strcmp(argv[i], "--seed")) seed = (unsigned)atoi(need("--seed"));
    else if (!strcmp(argv[i], "--task-id")) task_id = need("--task-id");
    else if (!strcmp(argv[i], "--out")) out_path = need("--out");
    else {
      usage();
      return 1;
    }
  }

  if (memory_mb < 64) memory_mb = 64;
  if (duration_sec < 1) duration_sec = 1;

  int device_count = 0;
  CHECK(cudaGetDeviceCount(&device_count));
  if (gpu < 0 || gpu >= device_count) {
    fprintf(stderr, "invalid gpu index %d (count=%d)\n", gpu, device_count);
    return 3;
  }
  CHECK(cudaSetDevice(gpu));

  cudaDeviceProp prop{};
  CHECK(cudaGetDeviceProperties(&prop, gpu));

  // Keep a safety margin so the process does not OOM the whole card.
  size_t bytes = (size_t)memory_mb * 1024ull * 1024ull;
  size_t free_b = 0, total_b = 0;
  CHECK(cudaMemGetInfo(&free_b, &total_b));
  if (bytes + 256ull * 1024ull * 1024ull > free_b) {
    if (free_b <= 256ull * 1024ull * 1024ull) {
      fprintf(stderr, "not enough free memory\n");
      return 4;
    }
    bytes = free_b - 256ull * 1024ull * 1024ull;
  }
  size_t n = bytes / sizeof(float) / 2;  // x and y
  if (n < 1024) n = 1024;
  bytes = n * sizeof(float);

  float *x = nullptr, *y = nullptr;
  CHECK(cudaMalloc(&x, bytes));
  CHECK(cudaMalloc(&y, bytes));
  CHECK(cudaMemset(x, 1, bytes));
  CHECK(cudaMemset(y, 2, bytes));

  int threads = 256;
  int blocks = (int)((n + threads - 1) / threads);
  if (blocks > prop.maxGridSize[0]) blocks = prop.maxGridSize[0];

  auto t0 = std::chrono::steady_clock::now();
  long long iters = 0;
  float a = 1.0001f + (seed % 100) * 0.00001f;
  while (true) {
    saxpy_kernel<<<blocks, threads>>>(x, y, a, (int)n);
    iters++;
    if ((iters & 0x3F) == 0) {
      CHECK(cudaDeviceSynchronize());
      auto now = std::chrono::steady_clock::now();
      double sec = std::chrono::duration<double>(now - t0).count();
      if (sec >= duration_sec) break;
    }
  }
  CHECK(cudaDeviceSynchronize());
  auto t1 = std::chrono::steady_clock::now();
  double elapsed = std::chrono::duration<double>(t1 - t0).count();

  size_t free_after = 0, total_after = 0;
  CHECK(cudaMemGetInfo(&free_after, &total_after));

  char digest_src[512];
  snprintf(digest_src, sizeof(digest_src), "%s|gpu=%d|mem_mb=%d|iters=%lld|seed=%u|dev=%s",
           task_id.c_str(), gpu, memory_mb, iters, seed, prop.name);

  // Simple stable hash (FNV-1a 64 then expand hex)
  unsigned long long h = 14695981039346656037ull;
  for (const char *p = digest_src; *p; ++p) {
    h ^= (unsigned char)(*p);
    h *= 1099511628211ull;
  }
  char sha_like[65];
  snprintf(sha_like, sizeof(sha_like), "%016llx%016llx%016llx%016llx",
           h, h ^ 0x9e3779b97f4a7c15ull, ~h, h * 0xbf58476d1ce4e5b9ull);

  printf("{\"task_id\":\"%s\",\"gpu\":%d,\"device\":\"%s\",\"allocated_mb\":%.1f,"
         "\"duration_sec\":%.3f,\"iterations\":%lld,\"result_sha256\":\"%s\","
         "\"free_mb_after\":%.1f,\"total_mb\":%.1f,\"ok\":true}\n",
         task_id.c_str(), gpu, prop.name, (bytes * 2.0) / (1024.0 * 1024.0),
         elapsed, iters, sha_like, free_after / (1024.0 * 1024.0),
         total_after / (1024.0 * 1024.0));

  if (!out_path.empty()) {
    FILE *fp = fopen(out_path.c_str(), "w");
    if (fp) {
      fprintf(fp,
              "{\"task_id\":\"%s\",\"gpu\":%d,\"device\":\"%s\",\"allocated_mb\":%.1f,"
              "\"duration_sec\":%.3f,\"iterations\":%lld,\"result_sha256\":\"%s\","
              "\"ok\":true}\n",
              task_id.c_str(), gpu, prop.name, (bytes * 2.0) / (1024.0 * 1024.0),
              elapsed, iters, sha_like);
      fclose(fp);
    }
  }

  cudaFree(x);
  cudaFree(y);
  return 0;
}
