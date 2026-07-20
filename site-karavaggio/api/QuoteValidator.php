<?php

declare(strict_types=1);

namespace Karavaggio\Api;

final class QuoteValidator
{
    private const DEFAULT_VALUE = 'Não informado';

    private const LIMITS = [
        'cnpj_pagador' => 32,
        'cnpj_origem' => 32,
        'origem' => 160,
        'cnpj_destino' => 32,
        'destino' => 160,
        'valor_nota' => 40,
        'volumes' => 20,
        'peso_bruto' => 40,
        'cubagem' => 500,
        'observacoes' => 2000,
    ];

    /** @return array<string, string> */
    public function validate(array $input): array
    {
        $quote = [];

        foreach (self::LIMITS as $field => $maxLength) {
            $value = $input[$field] ?? '';
            if (!is_string($value) && $value !== null) {
                throw new ApiException("O campo {$field} deve ser um texto.", 422);
            }

            $value = $this->sanitize((string) ($value ?? ''));
            if ($this->length($value) > $maxLength) {
                throw new ApiException(
                    "O campo {$field} deve possuir no máximo {$maxLength} caracteres.",
                    422
                );
            }

            $quote[$field] = $value !== '' ? $value : self::DEFAULT_VALUE;
        }

        return $quote;
    }

    private function sanitize(string $value): string
    {
        $value = str_replace("\0", '', $value);
        $value = preg_replace('/[\x01-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $value) ?? '';

        return trim($value);
    }

    private function length(string $value): int
    {
        return function_exists('mb_strlen') ? mb_strlen($value, 'UTF-8') : strlen($value);
    }
}
