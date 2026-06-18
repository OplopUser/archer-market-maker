FROM rust:1-bookworm AS builder

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 procps python3 screen zsh \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/target/release/archer-market-maker ./target/release/archer-market-maker
COPY config ./config
COPY dashboard ./dashboard
COPY docs ./docs
COPY scripts ./scripts

RUN mkdir -p logs

ENV PATH="/app/target/release:${PATH}"
CMD ["target/release/archer-market-maker", "--help"]
