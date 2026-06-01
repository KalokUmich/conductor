package calc

import (
    "testing"
    "github.com/stretchr/testify/assert"
)

func TestAdd(t *testing.T) {
    result := Add(2, 3)
    assert.Equal(t, 5, result)
}

func TestAddNegative(t *testing.T) {
    t.Run("negative numbers", func(t *testing.T) {
        result := Add(-1, -2)
        t.Fatal("should not reach here")
    })
}

func BenchmarkAdd(b *testing.B) {
    for i := 0; i < b.N; i++ {
        Add(1, 2)
    }
}
