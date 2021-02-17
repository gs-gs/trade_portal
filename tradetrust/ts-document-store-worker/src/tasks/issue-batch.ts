import { DocumentStore } from '@govtechsg/document-store/src/contracts/DocumentStore';
import { Wallet, utils, BigNumber} from 'ethers';
import { logger } from '../logger';
import { Batch } from './data';
import { Task } from './interfaces';
import { RetryError } from './errors';


class EthereumError extends Error{

  public static is(e:Error): boolean {
    return false;
  }

  public static throwIfIs(e: Error){
    if(this.is(e)){
      throw new this();
    }
  }
}


class TransactionTimeoutError extends EthereumError{
  static is(e:Error){
    return e.message.includes('timeout exceeded');
  }
}

class UnderpricedTransactionError extends EthereumError{
  static is(e:Error){
    return e.message.includes('replacement transaction underpriced');
  }
}

class HashDuplicationError extends EthereumError{
  static is(e: Error){
    return e.message.includes('Only hashes that have not been issued can be issued')
  }
}

interface IIssueBatchProps{
  wallet: Wallet,
  documentStore: DocumentStore,
  batch: Batch
  gasPriceMultiplier?: number,
  gasPriceLimitGwei?: number,
  transactionConfirmationThreshold?: number,
  transactionTimeoutSeconds?: number,
  attempts?: number,
  attemptsIntervalSeconds?: number
}

interface IIssueBatchState{
  attempt: number,
  pendingTransaction?: any,
  gasPriceMutiplier?: number,
  gasPriceLimit: BigNumber,
  gasPrice?: BigNumber,
  lastGasPrice?: BigNumber,
}


class IssueBatch implements Task<void>{

  private state: IIssueBatchState;
  private props: IIssueBatchProps;

  constructor(props: IIssueBatchProps){
    this.props = Object.assign({}, props);

    this.props.gasPriceMultiplier = this.props.gasPriceMultiplier??1.2;
    this.props.attempts = this.props.attempts??10;
    this.props.attemptsIntervalSeconds = this.props.attemptsIntervalSeconds??60;
    this.props.gasPriceLimitGwei = this.props.gasPriceLimitGwei??200;

    this.state = {
      attempt: 0,
      gasPriceLimit: utils.parseUnits(this.props.gasPriceLimitGwei!.toString(), 'gwei')
    }
  }

  async calculateGasPrice(gasPriceMultiplier: number): Promise<BigNumber>{
    const gasPriceWei = await this.props.wallet.getGasPrice();
    const gasPriceEtherMultiplied = parseFloat(utils.formatEther(gasPriceWei)) * gasPriceMultiplier;
    return utils.parseEther(gasPriceEtherMultiplied.toFixed(18));
  }


  async waitForTransaction(hash: string){
    logger.debug('waitForTransaction');
    logger.info(
      'Waiting for transaction to complete, hash:"%s", confirmations: %s, timeout: %s',
      hash,
      this.props.transactionConfirmationThreshold,
      this.props.transactionTimeoutSeconds
    )
    try{
      await this.props.wallet.provider.waitForTransaction(
        hash,
        this.props.transactionConfirmationThreshold,
        this.props.transactionTimeoutSeconds! * 1000
      )
    }catch(e){
      TransactionTimeoutError.throwIfIs(e);
      throw e;
    }
  }

  async sendTransaction(gasPrice: BigNumber){
    logger.debug('sendTransactionAndWait');
    const merkleRoot = '0x'+this.props.batch.merkleRoot;
    logger.info('Creating transaction for DocumentStore(%s).issue("%s")', this.props.documentStore.address, merkleRoot)
    try{
      const transaction = await this.props.documentStore.populateTransaction.issue(merkleRoot);
      transaction.gasLimit = await this.props.wallet.estimateGas(transaction);
      transaction.gasPrice = gasPrice;
      transaction.nonce = await this.props.wallet.getTransactionCount('latest');
      logger.info('Transaction created');
      logger.info(transaction);
      logger.info('Sending transaction...');
      // updating current pending transaction
      this.state.pendingTransaction = await this.props.wallet.sendTransaction(transaction);
      return this.state.pendingTransaction;
    }catch(e){
      // this error indicates that previos transaction was completed
      // but we still want to have certain number of confirmation for it
      // before we consider it successful
      if (HashDuplicationError.is(e)){
        // in this case pending transaction is not updated
        return this.state.pendingTransaction;
      }
      UnderpricedTransactionError.throwIfIs(e);
      throw e;
    }
  }

  async sendTransactionWithGasPriceAdjustment(){
    logger.debug('sendTransactionWithGasPriceAdjustment');
    // these values are in the state to restore after a critical failure
    this.state.gasPriceMutiplier = this.state.gasPriceMutiplier??1.0;
    this.state.gasPrice = this.state.gasPrice??(await this.calculateGasPrice(this.state.gasPriceMutiplier));
    this.state.lastGasPrice = this.state.lastGasPrice??this.state.gasPrice;
    logger.info('Gas price multiplier %s', this.state.gasPriceMutiplier);
    logger.info('Gas price %s gwei', utils.formatUnits(this.state.gasPrice, 'gwei'));
    logger.info('Last gas price %s gwei', utils.formatUnits(this.state.lastGasPrice, 'gwei'));

    while(true){
      try{
        // if gas price is less than limit
        if(this.state.gasPrice.lt(this.state.gasPriceLimit)){
          logger.info(
            'Sending new transaction, gas price[%s gwei] < gas limit[%s gwei]',
            utils.formatUnits(this.state.gasPrice, 'gwei'),
            utils.formatUnits(this.state.gasPriceLimit, 'gwei')
          );
          await this.sendTransaction(this.state.gasPrice);
          await this.waitForTransaction(this.state.pendingTransaction.hash);
          break;
        }else{
          // if gas price is greater than limit and no transaction sent yet or last sent transaction gas price
          // is less than limit
          if(!this.state.pendingTransaction || this.state.lastGasPrice.lt(this.state.gasPriceLimit)){
            logger.info(
              'Sending final transaction, last gas price[%s gwei] < gas limit[%s gwei] or no transaction sent yet',
              utils.formatUnits(this.state.lastGasPrice, 'gwei'),
              utils.formatUnits(this.state.gasPriceLimit, 'gwei')
            );
            this.state.lastGasPrice = this.state.gasPriceLimit;
            this.state.gasPrice = this.state.gasPriceLimit;
            await this.sendTransaction(this.state.gasPriceLimit);
            await this.waitForTransaction(this.state.pendingTransaction.hash);
            break;
          }else{
            logger.info(
              'Cannot increase gas price further, waiting for the last pending transaction %s',
              this.state.pendingTransaction.hash
            )
            await this.waitForTransaction(this.state.pendingTransaction.hash);
            break;
          }
        }
      }catch(e){
        try{
          if(e instanceof UnderpricedTransactionError || e instanceof TransactionTimeoutError){
            logger.info('Gas price insufficient, trying to increase it');
            if(this.state.gasPrice.lt(this.state.gasPriceLimit)){
              this.state.gasPriceMutiplier *= this.props.gasPriceMultiplier!;
              this.state.lastGasPrice = this.state.gasPrice;
              // can throw an error
              this.state.gasPrice = await this.calculateGasPrice(this.state.gasPriceMutiplier);
              logger.info('Gas price increased by a factor of %s', this.state.gasPriceMutiplier);
            }else{
              logger.info(
                'Gas price limit[%s gwei] reached, cannot increase gas price future',
                utils.formatUnits(this.state.gasPriceLimit, 'gwei')
              );
            }
          }else{
            throw e;
          }
        }catch(e){
          throw new RetryError(e);
        }
      }
    }
  }


  async sendTransactionRepeatedlyWithGasPriceAdjustment(){
    logger.debug('sendTransactionRepeatedlyWithGasPriceAdjustment');
    this.state.attempt = 0;
    while(true){
      try{
        logger.info('Issuing the batch, attempt %s/%s', this.state.attempt + 1, this.props.attempts);
        await this.sendTransactionWithGasPriceAdjustment();
        logger.info('The batch issued succesfully');
        this.props.batch.issued = true;
        return;
      }catch(e){
        // if process fails during gas price increase it can still pick up using this.state property
        if(e instanceof RetryError){
          this.state.attempt += 1;
          logger.error('An unexpected error occured');
          logger.error(e.source);
          if(this.state.attempt < this.props.attempts!){
            logger.info('Waiting %s seconds', this.props.attemptsIntervalSeconds);
            await new Promise(resolve=>setTimeout(resolve, this.props.attemptsIntervalSeconds! * 1000));
          }else{
            logger.error('Ran out of attempts, issuing failed');
            this.props.batch.issued = false;
            throw e.source;
          }
        }else{
          throw e;
        }
      }
    }
  }

  async next(){
    logger.debug('next');
    return this.sendTransactionRepeatedlyWithGasPriceAdjustment();
  }

  async start(){
    logger.debug('start');
    return this.next();
  }
}


export default IssueBatch;
