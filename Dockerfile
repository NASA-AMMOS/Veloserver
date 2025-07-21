FROM python:3.12-slim-bookworm

# Make sure apt-get commands work
RUN mv -i /etc/apt/trusted.gpg.d/debian-archive-*.asc  /root/
RUN ln -s /usr/share/keyrings/debian-archive-* /etc/apt/trusted.gpg.d/

WORKDIR /tmp/

# Install grib2json
RUN apt-get update && apt-get install -y git maven
RUN git clone https://github.com/jtroberts/grib2json.git && \
    cd grib2json && \
    mvn package && \
    tar -xvzf  target/grib2json-0.8.0-SNAPSHOT.tar.gz && \
    mv grib2json-0.8.0-SNAPSHOT/bin/* /usr/bin/ && \
    mv grib2json-0.8.0-SNAPSHOT/lib/* /usr/lib/ && \
    cd ..

# Install wgrib2
ENV DEBUG=true
ENV FC=gfortran
ENV CC=gcc
RUN apt-get update && apt-get install -y \
    cmake \
    ca-certificates \
    curl \
    wget \
    build-essential \
    bzip2 \
    tar \
    amqp-tools \
    openssh-client \
    gfortran \
    --no-install-recommends && rm -r /var/lib/apt/lists/* \
    && wget ftp://ftp.cpc.ncep.noaa.gov/wd51we/wgrib2/wgrib2.tgz -O /tmp/wgrib2.tgz \
    && mkdir -p /usr/local/grib2/ \
    && tar -xf /tmp/wgrib2.tgz -C /tmp/ \
    && rm -r /tmp/wgrib2.tgz \
    && mv /tmp/grib2/ /usr/local/grib2/ \
    && cd /usr/local/grib2/grib2 && make \
    && ln -s /usr/local/grib2/grib2/wgrib2/wgrib2 /usr/local/bin/wgrib2 \
    && apt-get -y autoremove build-essential

# Copy main files and create cache
COPY . /home/veloserver/
WORKDIR /home/veloserver
RUN mkdir -p /home/veloserver/cache

# Run pip to install Python dependencies
RUN pip3 install --upgrade pip
RUN pip3 install -r requirements.txt

# Run bottle server
EXPOSE 8104
CMD ["python3", "./server.py"]
