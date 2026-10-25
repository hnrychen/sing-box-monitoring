FROM golang:1.27.2 AS build
WORKDIR /src
COPY go-exporter/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /sing-box-exporter-go .

FROM scratch
COPY --from=build /sing-box-exporter-go /sing-box-exporter-go
ENV GOMAXPROCS=1 GOMEMLIMIT=40MiB
USER 65534:65534
EXPOSE 9119
ENTRYPOINT ["/sing-box-exporter-go"]
CMD ["--config", "/config/config.json"]
