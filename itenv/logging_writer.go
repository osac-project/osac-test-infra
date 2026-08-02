/*
Copyright (c) 2025 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

package itenv

import (
	"context"
	"errors"
	"log/slog"
	"strings"
)

// LogWriterBuilder contains the data and logic needed to create a writer that writes messages to a logger.
type LogWriterBuilder struct {
	logger  *slog.Logger
	level   slog.Level
	context context.Context
}

// LogWriter is an implementation of an io.Writer that writes messages to a slog.Logger.
type LogWriter struct {
	logger  *slog.Logger
	level   slog.Level
	context context.Context
}

// NewLogWriter creates a builder that can then be used to configure and create a new logging writer.
func NewLogWriter() *LogWriterBuilder {
	return &LogWriterBuilder{
		level: slog.LevelInfo,
	}
}

func (b *LogWriterBuilder) SetLogger(value *slog.Logger) *LogWriterBuilder {
	b.logger = value
	return b
}

func (b *LogWriterBuilder) SetLevel(value slog.Level) *LogWriterBuilder {
	b.level = value
	return b
}

func (b *LogWriterBuilder) SetContext(value context.Context) *LogWriterBuilder {
	b.context = value
	return b
}

func (b *LogWriterBuilder) Build() (result *LogWriter, err error) {
	if b.logger == nil {
		err = errors.New("logger is mandatory")
		return
	}
	result = &LogWriter{
		logger:  b.logger,
		level:   b.level,
		context: b.context,
	}
	return
}

func (w *LogWriter) Write(p []byte) (n int, err error) {
	text := strings.TrimRight(string(p), "\n")
	n = len(p)
	w.logger.LogAttrs(
		w.context,
		w.level,
		text,
	)
	return
}
