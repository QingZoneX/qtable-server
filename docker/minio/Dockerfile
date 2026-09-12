ARG MINIO_VERSION=RELEASE.2025-10-15T17-29-55Z

FROM golang:1.24-alpine AS builder
ARG MINIO_VERSION
RUN apk add --no-cache git ca-certificates && \
    GOBIN=/out go install github.com/minio/minio@${MINIO_VERSION}

FROM alpine:3.22
RUN apk add --no-cache ca-certificates && addgroup -S minio && adduser -S -G minio minio
COPY --from=builder /out/minio /usr/local/bin/minio
RUN mkdir -p /data && chown -R minio:minio /data
USER minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
