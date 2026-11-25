package egressfixture

import "github.com/google/uuid"

// NewID returns a fresh identifier. The dependency is the point of this
// fixture: it is what `go mod vendor` puts in the image.
func NewID() string {
	return uuid.NewString()
}
