package ginkgotiny_test

import (
	"testing"
	"time"

	"github.com/onsi/ginkgo/v2"
	"github.com/onsi/gomega"
)

func TestGinkgotiny(t *testing.T) {
	gomega.RegisterFailHandler(ginkgo.Fail)
	registerNativeReportCollector()
	ginkgo.RunSpecs(t, "Ginkgotiny Suite")
}

var _ = ginkgo.Describe("tiny", func() {
	ginkgo.It("passes on purpose", func() {
		gomega.Expect(1 + 1).To(gomega.Equal(2))
	})

	ginkgo.It("fails on purpose", func() {
		gomega.Expect(1 + 1).To(gomega.Equal(3))
	})

	ginkgo.It("sleeps for interruption tests", func() {
		time.Sleep(60 * time.Second)
	})
})
