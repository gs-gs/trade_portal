import _ from 'lodash';
import { logger } from 'src/logger';
import { getBatchedDocumentStoreTaskEnvConfig } from 'src/config';
import {SQS, S3} from 'src/aws';
import DOCUMENT_V2_JSON from './data/document.v2.json';

const config = getBatchedDocumentStoreTaskEnvConfig();

const S3Service = S3();
const SQSService = SQS();


async function clearBucket(Bucket: string){
  logger.debug('tests.utils.clearBucket "%s"', Bucket);
  let response: any = {};
  do{
    response = await S3Service.listObjectsV2({
      Bucket,
      ContinuationToken: response.NextContinuationToken
    }).promise();
    for(let object of response.Contents || []){
      if(object.Key){
        await S3Service.deleteObject({Key: object.Key, Bucket}).promise()
      }
    }
  }while(response.IsTruncated);
}

async function clearQueue(QueueUrl: string){
  logger.debug('tests.utils.clearQueue "%s"', QueueUrl);
  await SQSService.purgeQueue({QueueUrl}).promise();
}


function documentV2(overrides: object): any{
  const document = _.cloneDeep(DOCUMENT_V2_JSON);
  document.issuers[0].documentStore = config.DOCUMENT_STORE_ADDRESS;
  return Object.assign(document, overrides);
}


function generateDocumentsMap(documentsCount: number): Map<string, any>{
  const documents = new Map<string, any>();
  for(let documentIndex = 0; documentIndex < documentsCount; documentIndex++){
    const key = `document-key-${documentIndex}`
    const body = `document-body-${documentIndex}`
    documents.set(key, documentV2({body}));
  }
  return documents;
}


export {
  generateDocumentsMap,
  documentV2,
  clearQueue,
  clearBucket
}
