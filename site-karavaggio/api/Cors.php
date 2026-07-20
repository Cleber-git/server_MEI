<?php

declare(strict_types=1);

namespace Karavaggio\Api;

final class Cors
{
    private const ALLOWED_ORIGINS = [
        'https://karavaggio.com.br',
        'https://www.karavaggio.com.br',
        'https://homolog.karavaggio.com.br',
        'http://localhost:8000',
        'http://127.0.0.1:8000',
    ];

    public function handle(): void
    {
        $origin = $_SERVER['HTTP_ORIGIN'] ?? '';
        if ($origin !== '' && !in_array($origin, self::ALLOWED_ORIGINS, true)) {
            throw new ApiException('Origem não permitida.', 403);
        }

        if ($origin !== '') {
            header('Access-Control-Allow-Origin: ' . $origin);
            header('Vary: Origin');
            header('Access-Control-Allow-Methods: POST, OPTIONS');
            header('Access-Control-Allow-Headers: Content-Type');
            header('Access-Control-Max-Age: 86400');
        }

        if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
            http_response_code(204);
            exit;
        }
    }
}
