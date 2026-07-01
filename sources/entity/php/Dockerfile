FROM php:8.3-fpm-alpine

# Install nginx, Node.js, and required PHP extensions
RUN apk add --no-cache nginx whois curl nodejs npm

# Configure nginx
COPY docker/nginx.conf /etc/nginx/http.d/default.conf

# Configure PHP-FPM (multiple workers + timeout)
COPY docker/php-fpm.conf /usr/local/etc/php-fpm.d/zz-custom.conf

# Configure PHP-FPM for output flushing
RUN echo "output_buffering = Off" > /usr/local/etc/php/conf.d/streaming.ini && \
    echo "implicit_flush = On" >> /usr/local/etc/php/conf.d/streaming.ini && \
    echo "zlib.output_compression = Off" >> /usr/local/etc/php/conf.d/streaming.ini

# Copy application
COPY . /var/www/html
RUN cd /var/www/html && npm install playwright-core --save 2>/dev/null
RUN mkdir -p /var/www/html/cache && chown -R www-data:www-data /var/www/html/cache \
    && chown -R www-data:www-data /var/www/html/prompts \
    && touch /var/www/html/settings.json && chown www-data:www-data /var/www/html/settings.json

# Build info
ARG BUILD_SHA=dev
ARG BUILD_TIME=unknown
RUN echo "{\"sha\":\"${BUILD_SHA}\",\"time\":\"${BUILD_TIME}\"}" > /var/www/html/build.json

# Startup script
COPY docker/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 80

CMD ["/start.sh"]
