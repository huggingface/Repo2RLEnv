module example.com/egressfixture

go 1.23

// Pinned exactly: the image's module cache is built from this version, and the
// cache-miss control asks for a different one.
require github.com/google/uuid v1.6.0
