import { Tables } from '../../db/database.types';
import { BaseRepository } from './baseRepository';

export type StreamedText = Tables<'streamed_texts'>;
export type UserInfo = Tables<'user_info'>;

export type StreamedTextWithUserInfo = StreamedText & { user_info: UserInfo | null };

export type StreamedTextStatusFilter = 'all' | 'success' | 'failure';

const UNIQUE_VIOLATION = '23505';

export class StreamedTextsRepository extends BaseRepository {
  // (getAllStreamedTexts, lines 14-79 of the real file, left out: it is not on this path)


































































  /**
   * Get streamed text by UUID
   */
  async getStreamedTextByUuid(uuid: string): Promise<StreamedText | null> {
    const { data, error } = await this.supabase
      .schema('public')
      .from('streamed_texts')
      .select('*')
      .eq('uuid', uuid)
      .maybeSingle();

    if (error) {
      throw error;
    }

    return data;
  }

  /**
   * Upsert a streamed text record (create if doesn't exist, update if it does). An existing record is updated only if
   * it belongs to streamedText.user_id; otherwise this throws, so a chat UUID can't be taken over from another user.
   * @param streamedText The streamed text data to upsert
   * @returns The upserted streamed text record
   */
  async upsertStreamedText(
    streamedText: Partial<Omit<StreamedText, 'created_at' | 'blocked_at' | 'soft_refusals'>> &
      Pick<StreamedText, 'uuid' | 'user_id'>
  ): Promise<StreamedText> {
    if (!streamedText.uuid) {
      throw new Error('UUID is required for streamed text upsert');
    }

    // First check if the record exists
    const existingRecord = await this.getStreamedTextByUuid(streamedText.uuid);

    if (!existingRecord) {
      // Create new record
      const { data, error } = await this.supabase
        .schema('public')
        .from('streamed_texts')
        .insert({
          ...streamedText,
          created_at: new Date().toISOString(),
        })
        .select()
        .single();

      // A unique violation means another turn (or recordChatRefusal) created the chat after the check, so update it.
      if (error?.code !== UNIQUE_VIOLATION) {
        if (error) {
          throw error;
        }

        return data;
      }
    }

    // Update existing record
    const { data, error } = await this.supabase
      .schema('public')
      .from('streamed_texts')
      .update({
        ...streamedText,
        // Don't update created_at for existing records
        updated_at: new Date().toISOString(),
      })
      .eq('uuid', streamedText.uuid)
      .eq('user_id', streamedText.user_id)
      .select()
      .single();

    if (error) {
      throw error;
    }

    return data;
  }
}
