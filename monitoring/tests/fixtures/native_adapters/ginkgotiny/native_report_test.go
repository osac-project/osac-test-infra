package ginkgotiny_test

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/onsi/ginkgo/v2"
	"github.com/onsi/ginkgo/v2/reporters"
)

// The fixture stays unchanged unless collection is requested.
const reportDirEnv = "OSAC_CI_GINKGO_REPORT_DIR"

const reportTempPattern = ".ginkgo-report-*.json.partial"

// Error text and report content may contain credentials.
const reportCollectionFailed = "OSAC CI native report collection failed for this suite"

func registerNativeReportCollector() {
	dir := os.Getenv(reportDirEnv)
	if dir == "" {
		return
	}
	ginkgo.ReportAfterSuite("OSAC native report collector", func(report ginkgo.Report) {
		collectNativeReport(dir, report)
	})
}

func collectNativeReport(dir string, report ginkgo.Report) {
	temp, err := os.CreateTemp(dir, reportTempPattern)
	if err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
		return
	}
	tempName := temp.Name()
	if err := temp.Close(); err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
		return
	}
	if err := reporters.GenerateJSONReport(report, tempName); err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
		return
	}
	if err := publishNativeReport(tempName, dir); err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
	}
}

// Linking prevents overwrites. Failed cleanup leaves a visible partial.
func publishNativeReport(tempPath string, dir string) error {
	if err := os.Link(tempPath, filepath.Join(dir, finalReportName(tempPath))); err != nil {
		return err
	}
	return os.Remove(tempPath)
}

func finalReportName(tempPath string) string {
	name := strings.TrimPrefix(filepath.Base(tempPath), ".")
	return strings.TrimSuffix(name, ".partial")
}
