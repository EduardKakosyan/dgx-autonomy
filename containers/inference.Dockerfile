# syntax=docker/dockerfile:1.7
#
# llama.cpp llama-server for the DGX Spark GB10 (compute capability 12.1).
#
# - Pinned to the llama.cpp revision the research inspected.
# - Built for `121a-real`, the arch-specific target in NVIDIA's Spark playbook. The
#   build fails if the resulting CUDA code does not contain sm_121a.
# - NVIDIA's playbook also passes -DGGML_CURL=ON. That option does not exist at this
#   revision (LLAMA_CURL is deprecated and ignored), so it is left out on purpose.
# - The model is not in the image. The controller mounts the models dir read-only.

ARG CUDA_VERSION=13.0.2
ARG UBUNTU_VERSION=24.04

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build

ARG LLAMA_CPP_COMMIT=f95b0d95394d5e311ba8228689972843178c5e28
ARG CUDA_ARCHITECTURES=121a-real
ARG GGML_NATIVE=ON

RUN apt-get update \
 && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates libssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN set -eux; \
    git init -q llama.cpp; \
    cd llama.cpp; \
    git remote add origin https://github.com/ggml-org/llama.cpp.git; \
    git fetch -q --depth 1 origin "${LLAMA_CPP_COMMIT}"; \
    git checkout -q FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "${LLAMA_CPP_COMMIT}"

WORKDIR /src/llama.cpp
# libcuda.so comes from the host driver at run time (NVIDIA container runtime), so
# allow it to be unresolved at link time.
RUN set -eux; \
    cmake -B build \
      -DCMAKE_BUILD_TYPE=Release \
      -DGGML_CUDA=ON \
      -DGGML_NATIVE=${GGML_NATIVE} \
      -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES} \
      -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined; \
    cmake --build build --config Release --target llama-server -j"$(nproc)"

# Verify the architecture at build time instead of trusting the flag.
RUN set -eux; \
    mkdir -p /out/lib; \
    cp build/bin/llama-server /out/; \
    find build -name '*.so*' -exec cp -P {} /out/lib/ \; ; \
    cuda_lib="$(find /out/lib -name 'libggml-cuda.so*' -type f | head -n1)"; \
    test -n "$cuda_lib" || { echo "libggml-cuda.so was not built" >&2; exit 1; }; \
    cuobjdump --list-elf "$cuda_lib" | tee /out/cuda-elf.txt; \
    grep -q 'sm_121a' /out/cuda-elf.txt || { echo "no sm_121a code in $cuda_lib" >&2; exit 1; }; \
    echo "${LLAMA_CPP_COMMIT}" > /out/LLAMA_CPP_COMMIT

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libssl3t64 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/lib/ /opt/llama/lib/
COPY --from=build /out/llama-server /out/LLAMA_CPP_COMMIT /out/cuda-elf.txt /opt/llama/
ENV LD_LIBRARY_PATH=/opt/llama/lib:${LD_LIBRARY_PATH} \
    LLAMA_ARG_HOST=0.0.0.0 \
    LLAMA_ARG_PORT=8080

# The controller runs this image with --user 65534:65534 --cap-drop ALL.
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30m \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/opt/llama/llama-server"]
