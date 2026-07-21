<?php

declare(strict_types=1);

namespace Karavaggio\Api;

final class ContactValidator
{
    private const LIMITS = ['nome' => 160, 'email' => 254, 'mensagem' => 4000];

    public function validate(array $input): array
    {
        $contact = [];
        foreach (self::LIMITS as $field => $maxLength) {
            $value = $input[$field] ?? null;
            if (!is_string($value)) {
                throw new ApiException("O campo {$field} é obrigatório.", 422);
            }
            $value = trim(str_replace("\0", '', $value));
            if ($value === '') {
                throw new ApiException("O campo {$field} é obrigatório.", 422);
            }
            $length = function_exists('mb_strlen') ? mb_strlen($value, 'UTF-8') : strlen($value);
            if ($length > $maxLength) {
                throw new ApiException("O campo {$field} deve possuir no máximo {$maxLength} caracteres.", 422);
            }
            $contact[$field] = $value;
        }

        if (filter_var($contact['email'], FILTER_VALIDATE_EMAIL) === false) {
            throw new ApiException('Informe um e-mail válido.', 422);
        }
        return $contact;
    }
}
