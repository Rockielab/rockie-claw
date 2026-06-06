package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

var errMaterializeSecretMissing = errors.New("secret is missing")

type materializeSecretResponse struct {
	Name     string `json:"name"`
	Category string `json:"category"`
	Path     string `json:"path"`
	Mode     string `json:"mode"`
}

func materializeSecret(ctx context.Context, name string) (materializeSecretResponse, error) {
	if !secretNameRE.MatchString(name) {
		return materializeSecretResponse{}, fmt.Errorf("invalid secret name %q", name)
	}

	tenant := tenantID()
	metadata, err := cachedCandidateMetadata(ctx, tenant, []string{name})
	if err != nil {
		return materializeSecretResponse{}, err
	}
	category, known := metadata[name]
	if !known {
		return materializeSecretResponse{}, errMaterializeSecretMissing
	}
	if category != "ssh_key" {
		return materializeSecretResponse{}, fmt.Errorf("secret %q has category %q; only ssh_key can be materialized", name, category)
	}

	resolved, err := brokerSecretsClient.Resolve(ctx, tenant, []string{name}, "materialize_secret")
	if err != nil {
		return materializeSecretResponse{}, err
	}
	if err := validateResolvedExactSet([]string{name}, resolved, metadata); err != nil {
		if strings.Contains(err.Error(), " is missing") {
			return materializeSecretResponse{}, errMaterializeSecretMissing
		}
		return materializeSecretResponse{}, err
	}

	sshDir := filepath.Join(os.Getenv("HOME"), ".ssh")
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		return materializeSecretResponse{}, fmt.Errorf("create ssh dir: %w", err)
	}
	if err := os.Chmod(sshDir, 0o700); err != nil {
		return materializeSecretResponse{}, fmt.Errorf("chmod ssh dir: %w", err)
	}
	secretsDir := filepath.Join(sshDir, "rockie-secrets")
	if err := os.MkdirAll(secretsDir, 0o700); err != nil {
		return materializeSecretResponse{}, fmt.Errorf("create rockie secrets dir: %w", err)
	}
	if err := os.Chmod(secretsDir, 0o700); err != nil {
		return materializeSecretResponse{}, fmt.Errorf("chmod rockie secrets dir: %w", err)
	}

	path := filepath.Join(secretsDir, sanitizedSecretFilename(name))
	tmp, err := os.CreateTemp(secretsDir, ".materialize-*")
	if err != nil {
		return materializeSecretResponse{}, fmt.Errorf("create temp secret file: %w", err)
	}
	tmpName := tmp.Name()
	removeOnError := true
	defer func() {
		if removeOnError {
			_ = os.Remove(tmpName)
		}
	}()

	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return materializeSecretResponse{}, fmt.Errorf("chmod temp secret file: %w", err)
	}
	if _, err := tmp.WriteString(resolved.Values[name]); err != nil {
		_ = tmp.Close()
		return materializeSecretResponse{}, fmt.Errorf("write secret file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return materializeSecretResponse{}, fmt.Errorf("close secret file: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return materializeSecretResponse{}, fmt.Errorf("install secret file: %w", err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		return materializeSecretResponse{}, fmt.Errorf("chmod secret file: %w", err)
	}
	removeOnError = false

	return materializeSecretResponse{
		Name:     name,
		Category: category,
		Path:     path,
		Mode:     "0600",
	}, nil
}

func sanitizedSecretFilename(name string) string {
	var b strings.Builder
	for _, ch := range strings.ToLower(name) {
		if (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') {
			b.WriteRune(ch)
		} else {
			b.WriteByte('_')
		}
	}
	out := strings.Trim(b.String(), "_")
	if out == "" {
		out = "tenant_ssh_key"
	}
	sum := sha256.Sum256([]byte(name))
	return out + "-" + hex.EncodeToString(sum[:])[:12]
}
