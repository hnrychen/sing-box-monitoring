# Pass the same pinned Grafana image/version used by your existing deployment.
ARG BASE_IMAGE=grafana/grafana
FROM ${BASE_IMAGE}
USER root
COPY TwemojiCountryFlags.woff2 /usr/share/grafana/public/fonts/TwemojiCountryFlags.woff2
COPY FLAG-FONT-LICENSE.md /usr/share/grafana/public/fonts/FLAG-FONT-LICENSE.md
COPY grafana_flags.css /usr/share/grafana/public/build/country-flags.v3.css
RUN sed -i '/<\/head>/i\    <link rel="stylesheet" href="[[.AppSubUrl]]/public/build/country-flags.v3.css" />' /usr/share/grafana/public/views/index.html
USER 472
