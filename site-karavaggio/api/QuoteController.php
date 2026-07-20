<?php

declare(strict_types=1);

namespace Karavaggio\Api;

use JsonException;
use stdClass;

final class QuoteController
{
    public function __construct(
        private readonly QuoteValidator $validator,
        private readonly EmailService $emailService
    ) {
    }

    public function handle(): void
    {
        if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
            header('Allow: POST, OPTIONS');
            throw new ApiException('Método não permitido.', 405);
        }

        $contentType = strtolower($_SERVER['CONTENT_TYPE'] ?? '');
        if (!str_starts_with($contentType, 'application/json')) {
            throw new ApiException('Content-Type deve ser application/json.', 415);
        }

        $rawBody = file_get_contents('php://input');
        try {
            $input = json_decode($rawBody !== false ? $rawBody : '', false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new ApiException('JSON inválido.', 422);
        }

        if (!$input instanceof stdClass) {
            throw new ApiException('O corpo da requisição deve ser um objeto JSON.', 422);
        }

        $quote = $this->validator->validate((array) $input);
        $this->emailService->sendQuote($quote);

        Response::json(['message' => 'Cotação enviada com sucesso.']);
    }
}
