<?php

declare(strict_types=1);

namespace Karavaggio\Api;

use JsonException;
use stdClass;

final class ContactController
{
    public function __construct(private readonly ContactValidator $validator, private readonly SacEmailService $emailService) {}

    public function handle(): void
    {
        if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
            header('Allow: POST, OPTIONS');
            throw new ApiException('Método não permitido.', 405);
        }
        if (!str_starts_with(strtolower($_SERVER['CONTENT_TYPE'] ?? ''), 'application/json')) {
            throw new ApiException('Content-Type deve ser application/json.', 415);
        }
        try {
            $input = json_decode((string) file_get_contents('php://input'), false, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new ApiException('JSON inválido.', 422);
        }
        if (!$input instanceof stdClass) throw new ApiException('O corpo da requisição deve ser um objeto JSON.', 422);

        $this->emailService->send($this->validator->validate((array) $input));
        Response::json(['message' => 'Mensagem enviada ao SAC com sucesso.']);
    }
}
