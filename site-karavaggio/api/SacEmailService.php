<?php

declare(strict_types=1);

namespace Karavaggio\Api;

use JsonException;

final class SacEmailService
{
    private const ENDPOINT = 'https://api.resend.com/emails';
    private const SENDER = 'cotacao@karavaggio.com.br';
    private const RECIPIENT = 'sac@karavaggio.com.br';

    public function send(array $contact): void
    {
        $apiKey = trim((string) (getenv('RESEND_API_KEY') ?: ''));
        if ($apiKey === '') throw new ApiException('Serviço de e-mail não configurado.', 503);
        if (!function_exists('curl_init')) throw new ApiException('Serviço de e-mail indisponível.', 503);

        try {
            $payload = json_encode([
                'from' => 'Site Karavaggio <' . self::SENDER . '>',
                'to' => [self::RECIPIENT],
                'reply_to' => $contact['email'],
                'subject' => 'Mensagem para o SAC - Site Karavaggio',
                'text' => implode("\n", [
                    'Mensagem enviada pelo formulário de contato do site Karavaggio.',
                    '',
                    'Nome: ' . $contact['nome'],
                    'E-mail: ' . $contact['email'],
                    '',
                    'Mensagem:',
                    $contact['mensagem'],
                ]),
            ], JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        } catch (JsonException $exception) {
            error_log('Falha ao gerar mensagem do SAC: ' . $exception->getMessage());
            throw new ApiException('Falha ao enviar mensagem ao SAC.', 502);
        }

        $curl = curl_init(self::ENDPOINT);
        curl_setopt_array($curl, [
            CURLOPT_POST => true,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_TIMEOUT => 20,
            CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $apiKey, 'Content-Type: application/json'],
            CURLOPT_POSTFIELDS => $payload,
        ]);
        $responseBody = curl_exec($curl);
        $curlError = curl_error($curl);
        $statusCode = (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        curl_close($curl);

        if ($responseBody === false || $curlError !== '' || $statusCode < 200 || $statusCode >= 300) {
            error_log("Falha no envio ao SAC (HTTP {$statusCode}): {$curlError} {$responseBody}");
            throw new ApiException('Falha ao enviar mensagem ao SAC.', 502);
        }
    }
}
