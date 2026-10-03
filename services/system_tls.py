"""Use the operating system trust store for outbound HTTPS connections."""

import truststore


# Load this module before HTTP libraries so Python uses Windows CryptoAPI roots.
truststore.inject_into_ssl()
