<?php

declare(strict_types=1);

namespace Karavaggio\Api;

use JsonException;

final class EmailService
{
    private const RESEND_ENDPOINT = 'https://api.resend.com/emails';
    private const QUOTE_EMAIL = 'cotacao@karavaggio.com.br';

    public function sendQuote(array $quote): void
    {
        $apiKey = trim((string) (getenv('RESEND_API_KEY') ?: ''));

        if ($apiKey === '') {
            throw new ApiException('Serviço de e-mail não configurado.', 503);
        }

        if (!function_exists('curl_init')) {
            error_log('Extensão cURL do PHP não está disponível.');
            throw new ApiException('Serviço de e-mail indisponível.', 503);
        }

        try {
            $payload = json_encode([
                'from' => 'Site Karavaggio <' . self::QUOTE_EMAIL . '>',
                'to' => [self::QUOTE_EMAIL],
                'subject' => 'Solicitação de cotação - Site Karavaggio',
                'text' => $this->buildBody($quote),
            ], JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        } catch (JsonException $exception) {
            error_log('Falha ao gerar a mensagem da cotação: ' . $exception->getMessage());
            throw new ApiException('Falha ao enviar cotação.', 502);
        }

        $curl = curl_init(self::RESEND_ENDPOINT);
        curl_setopt_array($curl, [
            CURLOPT_POST => true,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_TIMEOUT => 20,
            CURLOPT_HTTPHEADER => [
                'Authorization: Bearer ' . $apiKey,
                'Content-Type: application/json',
            ],
            CURLOPT_POSTFIELDS => $payload,
        ]);

        $responseBody = curl_exec($curl);
        $curlError = curl_error($curl);
        $statusCode = (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        curl_close($curl);

        if ($responseBody === false || $curlError !== '') {
            error_log('Falha de conexão com o Resend: ' . $curlError);
            throw new ApiException('Falha ao enviar cotação.', 502);
        }

        if ($statusCode < 200 || $statusCode >= 300) {
            error_log("Resend respondeu HTTP {$statusCode}: {$responseBody}");
            throw new ApiException('Falha ao enviar cotação.', 502);
        }
    }

    private function buildBody(array $quote): string
    {
        return implode("\n", [
            'Solicitação de cotação enviada pelo site Karavaggio.',
            '',
            'Dados do pagador',
            'CNPJ do pagador: ' . $quote['cnpj_pagador'],
            '',
            'Dados de origem',
            'CNPJ de origem: ' . $quote['cnpj_origem'],
            'Cidade e estado de origem: ' . $quote['origem'],
            '',
            'Dados de destino',
            'CNPJ de destino: ' . $quote['cnpj_destino'],
            'Cidade e estado de destino: ' . $quote['destino'],
            '',
            'Dados da nota fiscal',
            'Valor total da Nota Fiscal: ' . $quote['valor_nota'],
            'Quantidade de volumes: ' . $quote['volumes'],
            'Peso bruto: ' . $quote['peso_bruto'],
            'Cubagem da mercadoria: ' . $quote['cubagem'],
            '',
            'Observações e comentários',
            $quote['observacoes'],
        ]);
    }
}
