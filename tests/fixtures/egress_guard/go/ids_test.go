package egressfixture

import (
	"testing"

	"github.com/google/uuid"
)

func TestNewIDIsAValidUUID(t *testing.T) {
	if _, err := uuid.Parse(NewID()); err != nil {
		t.Fatalf("NewID is not a UUID: %v", err)
	}
}
