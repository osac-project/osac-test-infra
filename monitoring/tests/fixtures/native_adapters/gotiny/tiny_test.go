package gotiny

import (
	"testing"
	"time"
)

func TestPassAlways(t *testing.T) {
	t.Parallel()
	if 1+1 != 2 {
		t.Fatal("math broke")
	}
}

func TestFailAlways(t *testing.T) {
	t.Fatal("this test fails on purpose")
}

func TestSlowInterruptible(t *testing.T) {
	time.Sleep(60 * time.Second)
}
