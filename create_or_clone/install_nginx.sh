set -e # Fai on any error
nginxVersion="1.12.2"
wget http://nginx.org/download/nginx-$nginxVersion.tar.gz
tar -xzf nginx-$nginxVersion.tar.gz
ln -sf nginx-$nginxVersion nginx
cd nginx
yum -y install gcc gcc-c++ make zlib-devel pcre-devel openssl-devel
./configure --user=nginx \
--group=nginx --prefix=/etc/nginx --sbin-path=/usr/sbin/nginx \
--conf-path=/etc/nginx/nginx.conf --pid-path=/var/run/nginx.pid \
--lock-path=/var/run/nginx.lock --error-log-path=/var/log/nginx/error.log \
--http-log-path=/var/log/nginx/access.log --with-http_gzip_static_module \
--with-http_stub_status_module --with-http_ssl_module --with-pcre \
--with-file-aio --with-http_realip_module --without-http_scgi_module \
--without-http_uwsgi_module --without-http_fastcgi_module --with-stream
make
make install
echo "Adding nginx user..."
useradd -r nginx
rm -rf /etc/init.d/nginx
echo "Checking /etc/init.d/nginx..."
ln -s /var/build/jenkins/etc.init.d.nginx /etc/init.d/nginx
rm -rf /etc/nginx/nginx.conf
ln -s /var/build/jenkins/nginx.conf /etc/nginx/nginx.conf
chmod +x /var/build/jenkins/etc.init.d.nginx
chkconfig --add nginx
chkconfig --level 345 nginx on
service nginx start
